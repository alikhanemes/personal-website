"""Private Cloudflare administration. Secrets travel only through stdin/HTTPS.

Management credentials live in the OS credential store and the host's secret
settings. Requests use signed POSTs. There is no public import URL token,
credential file, or browser-storage credential.
"""
import argparse
import getpass
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
import zipfile

from werkzeug.security import generate_password_hash

ROOT = Path(__file__).resolve().parents[1]
WRANGLER = ROOT / 'node_modules/.bin/wrangler.cmd'
CONFIG = ROOT / 'cloudflare/wrangler.jsonc'
_AUTH_ENV = None


def login_environment():
    global _AUTH_ENV
    if _AUTH_ENV is None:
        env = dict(os.environ, CLOUDFLARE_AUTH_USE_KEYRING='true')
        # Resolve OAuth separately from secret stdin. Wrangler's interactive
        # login can work while a piped secret command fails to load that login.
        # The supported token command hands it to the child only in memory.
        result = subprocess.run([str(WRANGLER), 'auth', 'token', '--json'], cwd=ROOT,
                                capture_output=True, text=True, encoding='utf-8', errors='replace', env=env)
        try:
            value = json.loads(result.stdout)
            token = value['token']
            if result.returncode or not isinstance(token, str) or not token:
                raise ValueError
        except (ValueError, KeyError, TypeError):
            raise RuntimeError('Cloudflare login is unavailable. Run .\\node_modules\\.bin\\wrangler.cmd login --use-keyring in this same PowerShell window, then retry.') from None
        _AUTH_ENV = dict(env, CLOUDFLARE_API_TOKEN=token)
    return _AUTH_ENV


def wrangler(args, secret=None, *, quiet=False):
    # No secret enters a command argument, shell string, or temporary file.
    result = subprocess.run([str(WRANGLER), *args, '--config', str(CONFIG)], cwd=ROOT,
                            input=secret + '\n' if secret is not None else None,
                            text=True, capture_output=True, encoding='utf-8', errors='replace',
                            env=login_environment())
    if result.returncode:
        raise RuntimeError('Cloudflare command failed:\n' + result.stdout + result.stderr)
    if not quiet:
        print(result.stdout)


def call(url, action, body, secret):
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.path not in {'', '/'} or parsed.query or parsed.fragment or parsed.username:
        raise ValueError('Use the HTTPS site origin, without a path or query.')
    path = '/__cloudflare/' + action
    stamp = str(int(time.time()))
    nonce = secrets.token_hex(16)
    message = '\n'.join(('POST', path, stamp, nonce, hashlib.sha256(body).hexdigest()))
    signature = hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()
    request = Request(url.rstrip('/') + path, data=body, method='POST',
                      headers={'Content-Type': 'application/octet-stream', 'X-Blog-Time': stamp,
                               'X-Blog-Nonce': nonce, 'X-Blog-Signature': signature,
                               'User-Agent': 'AlikhanBlogAdmin/1.0', 'Accept': 'application/json, application/zip'})
    with urlopen(request, timeout=90) as response:
        return response.read()


def administration_secret(url):
    host = urlsplit(url).hostname
    if not host:
        raise ValueError('A site hostname is required.')
    helper = ROOT / 'scripts/cloudflare-keyring.cjs'
    result = subprocess.run(['node', str(helper), 'get', host], capture_output=True, text=True)
    value = result.stdout.strip()
    if not value:
        value = secrets.token_urlsafe(48)
        result = subprocess.run(['node', str(helper), 'set', host], input=value,
                                capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError('Could not save the administration credential in the OS credential store.')
    return value


def run(args):
    if args.command == 'set-admin':
        login_environment()
        password = getpass.getpass('Production admin password (at least 12 characters): ')
        repeated = getpass.getpass('Confirm password: ')
        if password != repeated or len(password) < 12:
            raise ValueError('Passwords must match and contain at least 12 characters.')
        encoded = generate_password_hash(password, method='pbkdf2:sha256:1000000')
        wrangler(['secret', 'put', 'BLOG_ADMIN_PASSWORD_HASH'], encoded, quiet=True)
        print('Production password configured. Previous hosted sessions are revoked on the next request. Local credentials are unchanged.')
        return
    if not args.url:
        raise ValueError('--url is required for this command.')
    body = b'{}'
    if args.command in {'import', 'restore'}:
        if not args.source:
            raise ValueError('--source must name a verified private backup directory.')
        directory = Path(args.source).resolve()
        sys.path.insert(0, str(ROOT))
        import operations
        from app import create_app
        import shutil
        from uuid import uuid4
        (ROOT / '.qa').mkdir(exist_ok=True)
        temporary = ROOT / '.qa' / ('verify-cloudflare-import-' + uuid4().hex)
        temporary.mkdir()
        try:
            app = create_app({'DATA_DIR': temporary})
            with app.app_context():
                operations.verify_backup(directory)
        finally:
            shutil.rmtree(temporary)
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
            for path in directory.rglob('*'):
                if path.is_file():
                    name = path.relative_to(directory).as_posix()
                    if name in {'blog.sqlite3', 'manifest.json', 'deletion-ledger.json'} or name.startswith('uploads/'):
                        archive.write(path, name)
        body = output.getvalue()
    if args.command == 'review':
        body = json.dumps({'approve': args.approve}).encode()
    secret = administration_secret(args.url)
    synchronized = False
    # Binding changes may reach a Durable Object shortly after deployment.
    # Synchronize only after a rejected credential; normal commands need no
    # redeployment. Every retry receives a fresh nonce.
    for attempt in range(16):
        try:
            result = call(args.url, args.command, body, secret)
            break
        except HTTPError as error:
            if error.code != 404 or attempt == 15:
                raise
            if not synchronized:
                wrangler(['secret', 'put', 'BLOG_MAINTENANCE_SECRET'], secret, quiet=True)
                synchronized = True
            time.sleep(2)
    if args.command == 'backup':
        if not args.output:
            raise ValueError('--output must name a new private ZIP file.')
        target = Path(args.output).resolve()
        # Never write a backup into code/static or overwrite an older one.
        if target.exists() or not (ROOT / '.local-backups').resolve() in target.parents:
            raise ValueError('Choose a new ZIP path inside .local-backups/.')
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open('xb') as stream:
            stream.write(result)
        print('Private backup saved: ' + str(target))
    else:
        print(json.dumps(json.loads(result), indent=2, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['set-admin', 'status', 'import', 'restore', 'review', 'backup'])
    parser.add_argument('--url')
    parser.add_argument('--source')
    parser.add_argument('--output')
    parser.add_argument('--approve', action='store_true')
    try:
        run(parser.parse_args())
    except HTTPError as error:
        print(f'Hosted request failed ({error.code}): ' + error.read(8192).decode('utf-8', errors='replace'), file=sys.stderr)
        sys.exit(1)
    except (ValueError, OSError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
