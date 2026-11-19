"""Archive/delete storage boundaries, shared media and retryable cleanup."""

from io import BytesIO
import json
from pathlib import Path
import shutil
import sqlite3
import unittest
from unittest.mock import patch
from uuid import uuid4

from flask import Flask
from PIL import Image
from werkzeug.datastructures import FileStorage

import media
import posts


def document(text="Article", upload_id=None):
    content = [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]
    if upload_id:
        content.append({"type": "image", "attrs": {"uploadId": upload_id, "alt": "Shared image", "caption": "Caption"}})
    return {"type": "doc", "content": content}


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(__file__).resolve().parents[1] / ".qa" / "tests" / uuid4().hex
        self.directory.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.directory)
        self.app = Flask(__name__)
        self.app.config.update(DATA_DIR=str(self.directory), DATABASE=str(self.directory / "blog.sqlite3"))
        posts.init_app(self.app)
        context = self.app.app_context()
        context.push()
        self.addCleanup(context.pop)
        posts.init_db()

    def upload(self):
        stream = BytesIO()
        Image.new("RGB", (8, 8), "white").save(stream, format="PNG")
        stream.seek(0)
        return media.save_upload(FileStorage(stream, filename="example.png"))

    def draft(self, title="Disposable article", upload_id=None):
        draft = posts.new_draft(title)
        return posts.save_edit(draft["id"], title, document(title, upload_id), draft["version"])

    def publish(self, title="Disposable article", upload_id=None):
        draft = self.draft(title, upload_id)
        return posts.publish_edit(draft["id"], draft["version"])

    def test_archive_hides_main_permalink_and_media_preserving_working_copy(self):
        previous = self.publish("Previous article")
        upload = self.upload()
        published = self.publish("Newest article", upload["id"])
        working = posts.save_edit(published["id"], "Working title", document("Unpublished change"), published["version"])
        archived = posts.archive_post(working["id"], working["version"])
        self.assertEqual(posts.latest_published()["slug"], previous["slug"])
        self.assertIsNone(posts.published_by_slug(published["slug"]))
        self.assertFalse(media.public_upload(upload["id"]))
        self.assertIsNotNone(media.get_upload(upload["id"]))
        self.assertEqual(archived["status"], "archived")
        self.assertEqual(archived["title"], "Working title")
        self.assertEqual(archived["body"], working["body"])
        self.assertEqual(archived["published_at"], published["published_at"])
        self.assertEqual(archived["version"], working["version"] + 1)
        self.assertIn("archived", [row["status"] for row in posts.editor_list_posts()])
        with self.assertRaises(ValueError):
            posts.publish_edit(archived["id"], archived["version"])
        restored = posts.restore_post(archived["id"], archived["version"])
        public = posts.published_by_slug(published["slug"])
        self.assertEqual(public["title"], "Newest article")
        self.assertEqual(restored["title"], "Working title")
        self.assertEqual(public["published_at"], published["published_at"])
        self.assertTrue(media.public_upload(upload["id"]))
        self.assertEqual(posts.latest_published()["slug"], published["slug"])

    def test_archive_restore_and_delete_reject_stale_versions_and_drafts_cannot_archive(self):
        draft = self.draft()
        with self.assertRaises(ValueError):
            posts.archive_post(draft["id"], draft["version"])
        post = posts.publish_edit(draft["id"], draft["version"])
        with self.assertRaises(posts.EditConflict):
            posts.archive_post(post["id"], post["version"] - 1)
        archived = posts.archive_post(post["id"], post["version"])
        with self.assertRaises(posts.EditConflict):
            posts.restore_post(post["id"], post["version"])
        with self.assertRaises(posts.EditConflict):
            posts.permanently_delete_post(post["id"], "DELETE", post["version"])
        self.assertEqual(posts.get_edit(post["id"])["version"], archived["version"])

    def test_delete_requires_exact_confirmation_and_removes_original_and_working_images(self):
        public_image, working_image = self.upload(), self.upload()
        post = self.publish(upload_id=public_image["id"])
        edit = posts.save_edit(post["id"], "Working title", document("Working", working_image["id"]), post["version"])
        for confirmation in (None, "delete", " DELETE", "DELETE ", post["title"]):
            with self.assertRaises(ValueError):
                posts.permanently_delete_post(post["id"], confirmation, edit["version"])
        self.assertIsNotNone(posts.get_edit(post["id"]))
        result = posts.permanently_delete_post(post["id"], "DELETE", edit["version"])
        self.assertTrue(result["deleted"])
        self.assertEqual(result["cleanup"]["removed"], 2)
        self.assertEqual(result["cleanup"]["pending"], 0)
        self.assertIsNone(posts.get_edit(post["id"]))
        self.assertIsNone(posts.published_by_slug(post["slug"]))
        self.assertEqual(posts.get_db().execute("SELECT COUNT(*) FROM post_edits").fetchone()[0], 0)
        for upload in (public_image, working_image):
            self.assertFalse(Path(upload["path"]).exists())
            self.assertIsNone(media.get_upload(upload["id"]))
        self.assertEqual(posts.get_db().execute("SELECT slug FROM deleted_posts").fetchone()[0], post["slug"])
        with self.assertRaises(ValueError):
            posts.create_post(post["slug"], "Accidental recreation", document())

    def test_shared_media_is_public_only_while_a_public_article_references_it(self):
        upload = self.upload()
        first = self.publish("First", upload["id"])
        second = self.publish("Second", upload["id"])
        second = posts.archive_post(second["id"], second["version"])
        self.assertTrue(media.public_upload(upload["id"]))
        posts.permanently_delete_post(first["id"], "DELETE", first["version"])
        self.assertFalse(media.public_upload(upload["id"]))
        self.assertIsNotNone(media.get_upload(upload["id"]))
        posts.permanently_delete_post(second["id"], "DELETE", second["version"])
        self.assertIsNone(media.get_upload(upload["id"]))

    def test_draft_working_copy_retains_shared_image_and_fresh_unattached_upload_survives(self):
        upload, unattached = self.upload(), self.upload()
        first = self.publish("Public", upload["id"])
        second = self.draft("Private working copy", upload["id"])
        result = posts.permanently_delete_post(first["id"], "DELETE", first["version"])
        self.assertEqual(result["cleanup"]["removed"], 0)
        self.assertIsNotNone(media.get_upload(upload["id"]))
        self.assertIsNotNone(media.get_upload(unattached["id"]))
        posts.permanently_delete_post(second["id"], "DELETE", second["version"])
        self.assertIsNone(media.get_upload(upload["id"]))
        self.assertIsNotNone(media.get_upload(unattached["id"]))

    def test_failed_cleanup_is_private_durable_and_successfully_retried(self):
        upload = self.upload()
        draft = self.draft(upload_id=upload["id"])
        with patch.object(Path, "unlink", side_effect=PermissionError("Image is open in another program")):
            result = posts.permanently_delete_post(draft["id"], "DELETE", draft["version"])
        self.assertTrue(result["deleted"])
        self.assertIsNone(posts.get_edit(draft["id"]))
        self.assertEqual(result["cleanup"]["removed"], 0)
        self.assertEqual(result["cleanup"]["pending"], 1)
        self.assertEqual(result["cleanup"]["failed"], 1)
        self.assertTrue(Path(upload["path"]).exists())
        self.assertIsNone(media.get_upload(upload["id"]))
        self.assertFalse(media.public_upload(upload["id"]))
        job = posts.get_db().execute("SELECT * FROM media_cleanup").fetchone()
        self.assertEqual(job["attempts"], 1)
        self.assertIn("another program", job["last_error"])
        result = media.cleanup_media()
        self.assertEqual(result, {"removed": 1, "pending": 0, "failed": 0, "errors": []})
        self.assertFalse(Path(upload["path"]).exists())
        self.assertEqual(media.cleanup_media()["removed"], 0)

    def test_old_schema_migrates_without_changing_content_or_working_copy(self):
        legacy_path = self.directory / "legacy.sqlite3"
        with sqlite3.connect(legacy_path) as db:
            db.executescript("""
                CREATE TABLE posts (id INTEGER PRIMARY KEY, slug TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL, body TEXT NOT NULL, published_at TEXT);
                CREATE TABLE post_edits (post_id INTEGER PRIMARY KEY REFERENCES posts(id) ON DELETE CASCADE,
                    title TEXT NOT NULL, body TEXT NOT NULL, version INTEGER NOT NULL, updated_at TEXT NOT NULL);
            """)
            db.execute("INSERT INTO posts VALUES (1, 'existing', 'Public title', ?, '2026-01-01T00:00:00.000000Z')", (json.dumps(document("Public text")),))
            db.execute("INSERT INTO post_edits VALUES (1, 'Working title', ?, 9, '2026-01-02T00:00:00Z')", (json.dumps(document("Working text")),))
        db.close()
        posts.close_db()
        self.app.config["DATABASE"] = str(legacy_path)
        posts.init_db()
        posts.init_db()
        self.assertEqual(posts.published_by_slug("existing")["title"], "Public title")
        edit = posts.get_edit(1)
        self.assertEqual(edit["title"], "Working title")
        self.assertEqual(edit["version"], 9)
        self.assertIsNone(edit["archived_at"])


if __name__ == "__main__":
    unittest.main()
