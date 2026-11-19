"""Cloudflare entrypoint; all private/public routes reuse the Flask application."""
import base64
import hashlib
import hmac
import io
import json
from pathlib import Path
import re
import secrets
import shutil
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
import zipfile
import js

from jinja2 import DictLoader
from pyodide.ffi import create_once_callable, run_sync, to_js
from workers import DurableObject, Response, WorkerEntrypoint, fetch, wsgi

from _bundle import ASSETS, TEMPLATES
from app import create_app
from cloud_storage import CHUNK, DurableFiles


class SQLFiles:
    """Chunked private file storage with atomic multi-file replacement."""
    def __init__(self, ctx):
        self.ctx = ctx
        self.sql = ctx.storage.sql

    def initialize(self):
        self.sql.exec('CREATE TABLE IF NOT EXISTS blog_files (name TEXT PRIMARY KEY, size INTEGER NOT NULL)')
        self.sql.exec('CREATE TABLE IF NOT EXISTS blog_chunks (name TEXT NOT NULL, part INTEGER NOT NULL, body BLOB NOT NULL, PRIMARY KEY(name,part))')

    def files(self):
        return [(row['name'], int(row['size'])) for row in self.sql.exec('SELECT name,size FROM blog_files').toArray()]

    def read(self, name):
        rows = self.sql.exec('SELECT size FROM blog_files WHERE name=?', name).toArray()
        if not rows:
            return None
        chunks = self.sql.exec('SELECT body FROM blog_chunks WHERE name=? ORDER BY part', name).toArray()
        value = b''.join(bytes(row['body']) for row in chunks)
        if len(value) != int(rows[0]['size']):
            raise ValueError('Durable file integrity check failed.')
        return value

    def apply(self, changes):
        def write():
            for name, change in changes.items():
                if change is None:
                    self.sql.exec('DELETE FROM blog_chunks WHERE name=?', name)
                    self.sql.exec('DELETE FROM blog_files WHERE name=?', name)
                    continue
                size, chunks = change
                self.sql.exec('INSERT INTO blog_files(name,size) VALUES (?,?) ON CONFLICT(name) DO UPDATE SET size=excluded.size', name, size)
                self.sql.exec('DELETE FROM blog_chunks WHERE name=? AND part>=?', name, (size + CHUNK - 1) // CHUNK)
                for part, body in chunks.items():
                    self.sql.exec('INSERT INTO blog_chunks(name,part,body) VALUES (?,?,?) ON CONFLICT(name,part) DO UPDATE SET body=excluded.body',
                                  name, part, body)
        self.ctx.storage.transactionSync(create_once_callable(write))


class Blog(DurableObject):
    def __init__(self, ctx, env):
        super().__init__(ctx, env)
        self.app = None
        self.files = None

    def _setting(self, name, default=''):
        try:
            value = getattr(self.env, name)
            return default if value is None else str(value)
        except AttributeError:
            return default

    def _initialize(self):
        if self.app is not None:
            self.files._check()
            return
        root = Path('/tmp/alikhan-blog')
        # A fresh Object can reuse an isolate. Never retain a previous Object's
        # ephemeral database or images: authoritative state comes from storage.
        if root.exists():
            shutil.rmtree(root)
        self.files = DurableFiles(root, SQLFiles(self.ctx))
        code = Path('/tmp/alikhan-code/static')
        code.mkdir(parents=True, exist_ok=True)
        for name, value in ASSETS.items():
            (code / name).write_bytes(base64.b64decode(value))
        config = {
            'DATA_DIR': str(root), 'PRODUCTION': True, 'PUBLIC_ENABLED': self._setting('BLOG_PUBLIC_ENABLED') == 'true',
            'DATABASE_FACTORY': self.files.connect, 'MAX_IMAGE_PIXELS': 4_000_000,
            'UPLOAD_STAGE': self.files.stage_upload, 'UPLOAD_HYDRATE': self.files.hydrate_upload,
            'ADMIN_PASSWORD_HASH': self._setting('BLOG_ADMIN_PASSWORD_HASH'),
            'PASSWORD_CHECKER': self._check_password,
            'EMAIL_MODE': self._setting('BLOG_EMAIL_MODE', 'disabled'),
            'EMAIL_TEST_ADDRESS': self._setting('BLOG_EMAIL_TEST_ADDRESS'),
            'EMAIL_FROM': self._setting('BLOG_EMAIL_FROM'),
            'PUBLIC_BASE_URL': self._setting('BLOG_PUBLIC_BASE_URL'),
            'RESEND_API_KEY': self._setting('RESEND_API_KEY'),
            'EMAIL_TRANSPORT': self._send_email,
        }
        telegram = self._setting('BLOG_TELEGRAM_URL')
        if telegram:
            config['TELEGRAM_URL'] = telegram
        app = create_app(config)
        app.static_folder = str(code)
        app.jinja_loader = DictLoader(TEMPLATES)
        # Store only a fingerprint. Rotating the host secret revokes sessions,
        # including when a code deployment rolls back an older release.
        import auth
        import posts
        with app.app_context():
            db = posts.get_db()
            db.execute('CREATE TABLE IF NOT EXISTS cloudflare_settings (key TEXT PRIMARY KEY,value TEXT NOT NULL)')
            fingerprint = hashlib.sha256(config['ADMIN_PASSWORD_HASH'].encode()).hexdigest()
            old = db.execute("SELECT value FROM cloudflare_settings WHERE key='admin_fingerprint'").fetchone()
            if old is None or old[0] != fingerprint:
                auth.revoke_all()
                db.execute("INSERT OR REPLACE INTO cloudflare_settings VALUES ('admin_fingerprint',?)", (fingerprint,))
                db.commit()
        self.app = app

    def _check_password(self, encoded, password):
        try:
            method, salt, expected = encoded.split('$')
            if method != 'pbkdf2:sha256:1000000' or not re.fullmatch(r'[0-9a-f]{64}', expected):
                return False
            # Cloudflare's native APIs cap PBKDF2 at 100,000 iterations.
            # PyCryptodome's compiled implementation preserves the stronger
            # one-million-iteration hash without a custom crypto algorithm.
            from Crypto.Protocol.KDF import PBKDF2
            from Crypto.Hash import SHA256
            result = PBKDF2(password.encode('utf-8'), salt.encode('utf-8'), dkLen=32,
                            count=1000000, hmac_hash_module=SHA256)
            return secrets.compare_digest(result.hex(), expected)
        except (ValueError, TypeError):
            return False

    def _send_email(self, payload, idempotency_key):
        response = run_sync(fetch('https://api.resend.com/emails', method='POST',
            headers={'Authorization': 'Bearer ' + self._setting('RESEND_API_KEY'),
                     'Content-Type': 'application/json', 'Idempotency-Key': idempotency_key},
            body=json.dumps(payload, ensure_ascii=False), signal=js.AbortSignal.timeout(20000)))
        data = run_sync(response.text())
        if response.status >= 400:
            raise HTTPError('https://api.resend.com/emails', response.status, 'Provider rejected request', {}, None)
        return json.loads(data)

    async def fetch(self, request):
        async def serve():
            self._initialize()
            path = urlsplit(request.url).path
            if path.startswith('/__cloudflare/'):
                return await self._maintenance(request, path)
            # The bridge uses only Cloudflare's trusted client-IP header, never
            # a user-supplied forwarded IP. All requests arrive via HTTPS online.
            from workers import Request
            headers = {name: value for name, value in request.headers.items()
                       if name.lower() not in {'x-forwarded-for', 'x-forwarded-proto'}}
            headers['X-Forwarded-For'] = request.headers.get('CF-Connecting-IP', '127.0.0.1')
            headers['X-Forwarded-Proto'] = urlsplit(request.url).scheme
            safe = Request(request, headers=headers)
            response = await wsgi.fetch(self.app, safe, self.env)
            if not self.files.poisoned:
                self.files.persist()
            if self.files.poisoned:
                self.ctx.abort('Reload after failed durable commit')
            return response
        try:
            return await self.ctx.blockConcurrencyWhile(serve)
        finally:
            if self.files and self.files.poisoned:
                self.ctx.abort('Reload after failed durable commit')

    async def _maintenance(self, request, path):
        # CLI-only signed POSTs. No cookie credentials, URL tokens or CORS.
        # Body checksum, timestamp and one-use nonce provide request integrity,
        # replay protection and CSRF protection for this administration channel.
        secret = self._setting('BLOG_MAINTENANCE_SECRET')
        if not secret or request.method != 'POST' or request.headers.get('Origin'):
            return Response('Not found', status=404, headers={'Cache-Control': 'no-store'})
        try:
            stamp = request.headers.get('X-Blog-Time', '')
            nonce = request.headers.get('X-Blog-Nonce', '')
            signature = request.headers.get('X-Blog-Signature', '')
            if abs(time.time() - int(stamp)) > 120 or not re.fullmatch(r'[0-9a-f]{32}', nonce):
                raise ValueError
            # 32 MiB import limit; backups/imports remain private HTTP bodies.
            length = request.headers.get('Content-Length')
            if length and int(length) > 32 * 1024 * 1024:
                return Response('Import too large', status=413)
            body = bytes(await request.bytes())
            if len(body) > 32 * 1024 * 1024:
                return Response('Import too large', status=413)
            message = '\n'.join((request.method, path, stamp, nonce, hashlib.sha256(body).hexdigest()))
            expected = hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()
            if not secrets.compare_digest(expected, signature):
                raise ValueError
        except (ValueError, TypeError):
            return Response('Not found', status=404, headers={'Cache-Control': 'no-store'})
        import posts
        import operations
        with self.app.app_context():
            db = posts.get_db()
            db.execute('CREATE TABLE IF NOT EXISTS cloudflare_nonces (nonce TEXT PRIMARY KEY,expires_at REAL NOT NULL)')
            db.execute('DELETE FROM cloudflare_nonces WHERE expires_at<?', (time.time(),))
            try:
                db.execute('INSERT INTO cloudflare_nonces VALUES (?,?)', (nonce, time.time() + 300))
                db.commit()
            except Exception:
                db.rollback()
                return Response('Request already used', status=409)
            try:
                if path == '/__cloudflare/status':
                    summary = {'posts': posts.editor_list_posts(), 'held': (self.files.root / operations.MARKER).exists(),
                               'public_enabled': self.app.config['PUBLIC_ENABLED'],
                               'admin_configured': bool(self.app.config['ADMIN_PASSWORD_HASH'])}
                    return self._json(summary)
                if path in {'/__cloudflare/import', '/__cloudflare/restore'}:
                    # Initial transfer only. Restoring an existing site uses the
                    # held restore flow, and must merge its current tombstones.
                    if path.endswith('/import') and db.execute('SELECT 1 FROM posts UNION ALL SELECT 1 FROM deleted_posts LIMIT 1').fetchone():
                        return self._json({'error': 'Initial import requires an empty site. Use the backup/restore procedure for an existing site.'}, 409)
                    directory = Path('/tmp/blog-import-' + nonce)
                    directory.mkdir()
                    try:
                        with zipfile.ZipFile(io.BytesIO(body)) as archive:
                            if sum(info.file_size for info in archive.infolist()) > 32 * 1024 * 1024:
                                raise ValueError('Expanded import is too large.')
                            for info in archive.infolist():
                                name = info.filename
                                if info.is_dir():
                                    continue
                                if name not in {'blog.sqlite3', 'manifest.json', 'deletion-ledger.json'} and not re.fullmatch(r'uploads/[0-9a-f]{32}\.(?:png|jpg|webp)', name):
                                    raise ValueError('Unexpected backup file.')
                                target = directory / name
                                target.parent.mkdir(parents=True, exist_ok=True)
                                target.write_bytes(archive.read(info))
                        result = operations.restore_data(directory)
                        retained = {row[0] for row in posts.get_db().execute('SELECT filename FROM uploads')}
                        for name, _ in self.files.backend.files():
                            if name.startswith('uploads/') and name.split('/', 1)[1] not in retained:
                                self.files.stage_upload(name.split('/', 1)[1], None)
                        for image in (self.files.root / 'uploads').iterdir():
                            if image.name in retained:
                                self.files.stage_upload(image.name, image.read_bytes())
                        self.files.persist()
                        return self._json(result)
                    finally:
                        shutil.rmtree(directory)
                if path == '/__cloudflare/review':
                    for row in db.execute('SELECT filename FROM uploads'):
                        self.files.hydrate_upload(row[0], self.files.root / 'uploads' / row[0])
                    result = operations.review_restored_data(approve=json.loads(body or b'{}').get('approve') is True)
                    self.files.persist()
                    return self._json(result)
                if path == '/__cloudflare/backup':
                    for row in db.execute('SELECT filename FROM uploads'):
                        self.files.hydrate_upload(row[0], self.files.root / 'uploads' / row[0])
                    directory = Path('/tmp/blog-backup-' + nonce)
                    try:
                        operations.backup_data(directory)
                        output = io.BytesIO()
                        with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
                            for file in directory.rglob('*'):
                                if file.is_file():
                                    archive.write(file, file.relative_to(directory).as_posix())
                        return Response(output.getvalue(), headers={'Content-Type': 'application/zip', 'Cache-Control': 'no-store, private'})
                    finally:
                        if directory.exists():
                            shutil.rmtree(directory)
                return Response('Not found', status=404)
            except (ValueError, OSError, zipfile.BadZipFile) as error:
                return self._json({'error': str(error)}, 400)

    def _json(self, value, status=200):
        return Response(json.dumps(value), status=status, headers={'Content-Type': 'application/json', 'Cache-Control': 'no-store, private'})

    async def tick(self):
        async def process():
            self._initialize()
            import newsletter
            import media
            with self.app.app_context():
                newsletter.process_outbox(limit=5)
                media.cleanup_media()
        await self.ctx.blockConcurrencyWhile(process)


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        url = urlsplit(request.url)
        if url.hostname == 'www.alikhan-zarubay.com' or (url.hostname == 'alikhan-zarubay.com' and url.scheme != 'https'):
            location = 'https://alikhan-zarubay.com' + (url.path or '/')
            if url.query:
                location += '?' + url.query
            return Response('', status=308, headers={'Location': location, 'Cache-Control': 'no-store'})
        return await self.env.BLOG.getByName('personal-blog-v1').fetch(request)

    async def scheduled(self, controller, env, ctx):
        await self.env.BLOG.getByName('personal-blog-v1').tick()
