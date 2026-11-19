"""Durable backing for the existing SQLite file, without replacing its SQL API.

One named Durable Object serializes requests. SQLite still owns transactions and
constraints. Each commit atomically persists changed 64 KiB database chunks,
staged upload chunks and review/deletion sidecars in Cloudflare SQL storage.
Reads use the restored local file; uploaded files are hydrated only as needed.
An unsuccessful durable write poisons the instance: no later request can read
an unacknowledged local commit. The next instance restores the last durable state.
"""

import hashlib
from pathlib import Path
import re
import sqlite3

CHUNK = 64 * 1024
MAX_DATABASE = 16 * 1024 * 1024
MAX_MEDIA = 16 * 1024 * 1024
SIDECARS = ('restore-review-required.json', 'deletion-ledger.json')
MEDIA_NAME = re.compile(r'[0-9a-f]{32}\.(?:png|jpg|webp)')


class DurableWriteError(RuntimeError):
    pass


class DurableFiles:
    def __init__(self, root, backend):
        self.root = Path(root)
        self.backend = backend
        self.hashes = {}
        self.staged = {}
        self.poisoned = False
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / 'uploads').mkdir(exist_ok=True)
        backend.initialize()
        for name in ('blog.sqlite3', *SIDECARS):
            value = backend.read(name)
            path = self.root / name
            if value is not None:
                path.write_bytes(value)
                self._remember(name, value)
            else:
                path.unlink(missing_ok=True)

    def _check(self):
        if self.poisoned:
            raise DurableWriteError('Storage needs to reload after an interrupted write. Retry the request.')

    def _remember(self, name, value):
        self.hashes[name] = tuple(hashlib.sha256(value[i:i + CHUNK]).digest()
                                  for i in range(0, len(value), CHUNK))

    def stage_upload(self, filename, value):
        self._check()
        if not MEDIA_NAME.fullmatch(filename):
            raise ValueError('Invalid upload filename.')
        changes = dict(self.staged)
        changes['uploads/' + filename] = value
        media = {name: size for name, size in self.backend.files() if name.startswith('uploads/')}
        for name, body in changes.items():
            if body is None:
                media.pop(name, None)
            else:
                media[name] = len(body)
        if sum(media.values()) > MAX_MEDIA:
            raise ValueError('The 16 MiB image budget is full. Remove unused images or export a backup before expanding storage.')
        self.staged = changes

    def hydrate_upload(self, filename, path):
        self._check()
        if not MEDIA_NAME.fullmatch(filename):
            raise ValueError('Invalid upload filename.')
        name = 'uploads/' + filename
        value = self.staged.get(name) if name in self.staged else self.backend.read(name)
        if value is not None:
            Path(path).write_bytes(value)
        else:
            Path(path).unlink(missing_ok=True)

    def persist(self):
        self._check()
        files = dict(self.staged)
        for name in ('blog.sqlite3', *SIDECARS):
            path = self.root / name
            files[name] = path.read_bytes() if path.exists() else None
        if files['blog.sqlite3'] is not None and len(files['blog.sqlite3']) > MAX_DATABASE:
            self.poisoned = True
            raise DurableWriteError('The blog database reached its 16 MiB safety limit. Export a backup before expanding it.')
        # Metadata includes unloaded uploads. Count replacements once.
        media = {name: size for name, size in self.backend.files() if name.startswith('uploads/')}
        for name, value in files.items():
            if name.startswith('uploads/'):
                if value is None:
                    media.pop(name, None)
                else:
                    media[name] = len(value)
        if sum(media.values()) > MAX_MEDIA:
            self.poisoned = True
            raise DurableWriteError('This initial free-hosting setup allows 16 MiB of images. Remove unused images or export a backup before expanding storage.')
        changes = {}
        for name, value in files.items():
            if value is None:
                if name in dict(self.backend.files()):
                    changes[name] = None
                continue
            hashes = tuple(hashlib.sha256(value[i:i + CHUNK]).digest()
                           for i in range(0, len(value), CHUNK))
            old = self.hashes.get(name, ())
            if hashes != old:
                chunks = {i: value[i * CHUNK:(i + 1) * CHUNK] for i, h in enumerate(hashes)
                          if i >= len(old) or old[i] != h}
                changes[name] = (len(value), chunks)
        try:
            if changes:
                self.backend.apply(changes)
        except Exception as error:
            self.poisoned = True
            raise DurableWriteError('The durable save failed. Reload before continuing; your previous saved data remains available.') from error
        for name, value in files.items():
            if value is None:
                self.hashes.pop(name, None)
            else:
                self._remember(name, value)
        self.staged.clear()

    def connect(self, filename):
        self._check()
        owner = self

        class Connection(sqlite3.Connection):
            def commit(self):
                owner._check()
                if self.in_transaction:
                    size = self.execute('PRAGMA page_size').fetchone()[0] * self.execute('PRAGMA page_count').fetchone()[0]
                    if size > MAX_DATABASE:
                        self.rollback()
                        raise ValueError('The 16 MiB database budget is full. Export a backup before expanding storage.')
                super().commit()
                owner.persist()

            def rollback(self):
                super().rollback()
                owner.staged.clear()

        return sqlite3.connect(filename, factory=Connection)
