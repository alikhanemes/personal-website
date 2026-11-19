"""Storage contracts needed when the Worker process is evicted or redeployed."""
import copy
import importlib.util
from pathlib import Path
import sqlite3
import shutil
import unittest
from uuid import uuid4
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('cloudflare_storage_test', ROOT / 'cloudflare/storage.py')
storage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(storage)


class Backend:
    def __init__(self):
        self.entries = {}
        self.calls = []
        self.fail = False

    def initialize(self):
        pass

    def files(self):
        return [(name, size) for name, (size, _) in self.entries.items()]

    def read(self, name):
        if name not in self.entries:
            return None
        size, chunks = self.entries[name]
        return b''.join(chunks[i] for i in sorted(chunks))[:size]

    def apply(self, changes):
        next_entries = copy.deepcopy(self.entries)
        for name, value in changes.items():
            if value is None:
                next_entries.pop(name, None)
                continue
            size, chunks = value
            existing = next_entries.get(name, (0, {}))[1]
            existing = {i: part for i, part in existing.items() if i * storage.CHUNK < size}
            existing.update(chunks)
            next_entries[name] = (size, existing)
        if self.fail:
            raise OSError('Simulated storage outage before atomic commit')
        self.entries = next_entries
        self.calls.append(copy.deepcopy(changes))


class StorageTests(unittest.TestCase):
    def setUp(self):
        qa = ROOT / '.qa'
        qa.mkdir(exist_ok=True)
        self.directory = qa / ('cloudflare-storage-test-' + uuid4().hex)
        self.directory.mkdir()
        self.addCleanup(shutil.rmtree, self.directory)
        self.backend = Backend()
        self.files = storage.DurableFiles(self.directory, self.backend)
        self.path = self.directory / 'blog.sqlite3'

    def connect(self):
        db = self.files.connect(self.path)
        self.addCleanup(db.close)
        return db

    def test_committed_working_copy_survives_a_new_instance(self):
        db = self.connect()
        db.execute('CREATE TABLE drafts (id INTEGER PRIMARY KEY, version INTEGER, body TEXT)')
        db.execute("INSERT INTO drafts VALUES (1,7,'private writing')")
        db.commit()
        db.close()
        self.files = storage.DurableFiles(self.directory, self.backend)
        db = self.connect()
        self.assertEqual(db.execute('SELECT version,body FROM drafts').fetchone(), (7, 'private writing'))

    def test_rolled_back_save_does_not_become_durable(self):
        db = self.connect()
        db.execute('CREATE TABLE drafts (body TEXT)')
        db.commit()
        db.execute("INSERT INTO drafts VALUES ('must not persist')")
        self.files.stage_upload('a' * 32 + '.png', b'not committed')
        db.rollback()
        db.commit()
        self.assertIsNone(self.backend.read('uploads/' + 'a' * 32 + '.png'))
        db.close()
        self.files = storage.DurableFiles(self.directory, self.backend)
        self.assertEqual(self.connect().execute('SELECT COUNT(*) FROM drafts').fetchone()[0], 0)

    def test_failed_durable_commit_keeps_previous_data_and_fails_closed(self):
        db = self.connect()
        db.execute('CREATE TABLE drafts (body TEXT)')
        db.execute("INSERT INTO drafts VALUES ('previous save')")
        db.commit()
        before = self.backend.read('blog.sqlite3')
        db.execute("UPDATE drafts SET body='unacknowledged save'")
        self.backend.fail = True
        with self.assertRaises(storage.DurableWriteError):
            db.commit()
        self.assertEqual(self.backend.read('blog.sqlite3'), before)
        with self.assertRaises(storage.DurableWriteError):
            self.files.connect(self.path)
        db.close()
        self.backend.fail = False
        self.files = storage.DurableFiles(self.directory, self.backend)
        self.assertEqual(self.connect().execute('SELECT body FROM drafts').fetchone()[0], 'previous save')

    def test_image_and_database_are_atomic_and_image_is_loaded_lazily(self):
        db = self.connect()
        name = 'b' * 32 + '.png'
        db.execute('CREATE TABLE images (filename TEXT)')
        db.execute('INSERT INTO images VALUES (?)', (name,))
        self.files.stage_upload(name, b'image content' * 10000)
        db.commit()
        self.assertEqual(set(self.backend.calls[-1]), {'blog.sqlite3', 'uploads/' + name})
        image = self.directory / 'uploads' / name
        image.unlink(missing_ok=True)
        self.files.hydrate_upload(name, image)
        self.assertEqual(image.read_bytes(), b'image content' * 10000)
        self.files.stage_upload(name, None)
        db.execute('DELETE FROM images')
        db.commit()
        self.files.hydrate_upload(name, image)
        self.assertFalse(image.exists())
        self.assertIsNone(self.backend.read('uploads/' + name))

    def test_unchanged_commit_does_not_rewrite_storage(self):
        db = self.connect()
        db.execute('CREATE TABLE posts (body TEXT)')
        db.execute('INSERT INTO posts VALUES (?)', ('x' * (storage.CHUNK * 3),))
        db.commit()
        calls = len(self.backend.calls)
        db.commit()
        self.assertEqual(len(self.backend.calls), calls)
        db.execute('UPDATE posts SET body=?', ('x' * (storage.CHUNK * 3 - 1) + 'y',))
        db.commit()
        _, changed = self.backend.calls[-1]['blog.sqlite3']
        self.assertLess(len(changed), 4)

    def test_review_marker_and_deletion_history_survive_restart(self):
        marker = self.directory / 'restore-review-required.json'
        ledger = self.directory / 'deletion-ledger.json'
        marker.write_text('{"state":"review_required"}')
        ledger.write_text('{"deleted_posts":[{"slug":"removed"}]}')
        self.files.persist()
        marker.unlink()
        ledger.unlink()
        self.files = storage.DurableFiles(self.directory, self.backend)
        self.assertIn('review_required', marker.read_text())
        self.assertIn('removed', ledger.read_text())

    def test_database_limit_rejects_new_save_without_poisoning_previous_state(self):
        db = self.connect()
        db.execute('CREATE TABLE drafts (body TEXT)')
        db.commit()
        before = self.backend.read('blog.sqlite3')
        with patch.object(storage, 'MAX_DATABASE', len(before)):
            db.execute('INSERT INTO drafts VALUES (?)', ('x' * 100000,))
            with self.assertRaises(ValueError):
                db.commit()
        self.assertFalse(self.files.poisoned)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM drafts').fetchone()[0], 0)
        self.assertEqual(self.backend.read('blog.sqlite3'), before)

    def test_image_limit_rejects_upload_before_database_commit(self):
        with patch.object(storage, 'MAX_MEDIA', 4):
            with self.assertRaises(ValueError):
                self.files.stage_upload('c' * 32 + '.png', b'too large')
        self.assertFalse(self.files.poisoned)
        self.assertEqual(self.files.staged, {})


if __name__ == '__main__':
    unittest.main()
