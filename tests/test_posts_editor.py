"""Focused persistence and image publication boundaries, independent of UI."""

from io import BytesIO
from pathlib import Path
import shutil
import unittest
from uuid import uuid4

from flask import Flask
from PIL import Image
from werkzeug.datastructures import FileStorage

import media
import posts
from rendering import render_body


def body(text):
    return {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]}


def image_file(image_format="PNG", **options):
    stream = BytesIO()
    Image.new("RGB", (40, 30), (32, 128, 200)).save(stream, format=image_format, **options)
    stream.seek(0)
    return FileStorage(stream, filename="image." + image_format.lower())


class EditorStorageTests(unittest.TestCase):
    def setUp(self):
        self.data_dir = Path(__file__).resolve().parents[1] / ".qa" / "tests" / uuid4().hex
        self.data_dir.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.data_dir)
        self.app = Flask(__name__)
        self.app.config.update(DATA_DIR=str(self.data_dir), DATABASE=str(self.data_dir / "blog.sqlite3"))
        posts.init_app(self.app)
        self.context = self.app.app_context()
        self.context.push()
        self.addCleanup(self.context.pop)
        posts.init_db()

    def test_initialization_preserves_old_posts_and_working_edits(self):
        posts.create_post("existing-url", "Existing title", body("Existing body"), "2026-01-01T09:00:00Z")
        before = posts.published_by_slug("existing-url")
        edit = posts.get_edit(before["id"])
        saved = posts.save_edit(edit["id"], "Working title", body("Unsaved to public"), edit["version"])
        posts.init_db()
        self.assertEqual(posts.published_by_slug("existing-url"), before)
        self.assertEqual(posts.get_edit(edit["id"])["title"], saved["title"])
        self.assertEqual(posts.editor_list_posts()[0]["title"], "Working title")

    def test_autosave_compare_and_swap_rejects_stale_writes(self):
        edit = posts.new_draft()
        saved = posts.save_edit(edit["id"], "Newest save", body("Newest text"), 0)
        self.assertEqual(saved["version"], 1)
        with self.assertRaises(posts.EditConflict):
            posts.save_edit(edit["id"], "Out of order", body("Stale text"), 0)
        self.assertEqual(posts.get_edit(edit["id"])["title"], "Newest save")
        self.assertIsNone(posts.latest_published())
        self.assertIsNone(posts.published_by_slug(edit["slug"]))
        self.assertEqual(posts.older_published(), [])

    def test_explicit_publish_changes_main_and_update_preserves_date_and_url(self):
        posts.create_post("prior-main", "Prior Main", body("Prior body"), "2030-01-01T00:00:00Z")
        edit = posts.new_draft()
        saved = posts.save_edit(edit["id"], "New Main", body("Entire article"), edit["version"])
        self.assertEqual(posts.latest_published()["slug"], "prior-main")
        with self.assertRaises(posts.EditConflict):
            posts.publish_edit(edit["id"], 0)
        published = posts.publish_edit(edit["id"], saved["version"])
        self.assertEqual(posts.latest_published()["slug"], edit["slug"])
        self.assertEqual(posts.older_published()[0]["slug"], "prior-main")
        first_timestamp = published["published_at"]
        saved_update = posts.save_edit(edit["id"], "Working update", body("Unpublished changed body"), published["version"])
        public_before_update = posts.published_by_slug(edit["slug"])
        self.assertEqual(public_before_update["title"], "New Main")
        self.assertIn("Entire article", str(render_body(public_before_update["body"])))
        with self.assertRaises(posts.EditConflict):
            posts.publish_edit(edit["id"], published["version"])
        updated = posts.publish_edit(edit["id"], saved_update["version"])
        self.assertEqual(updated["slug"], edit["slug"])
        self.assertEqual(updated["published_at"], first_timestamp)
        self.assertEqual(posts.published_by_slug(edit["slug"])["title"], "Working update")

    def test_upload_privacy_tracks_published_body_only(self):
        upload = media.save_upload(image_file())
        upload_id = upload["id"]
        self.assertEqual(upload["mime_type"], "image/png")
        self.assertEqual((upload["width"], upload["height"]), (40, 30))
        self.assertTrue(Path(upload["path"]).is_file())
        self.assertTrue(Path(upload["path"]).is_relative_to(self.data_dir / "uploads"))
        self.assertFalse(media.public_upload(upload_id))
        edit = posts.new_draft()
        image_body = {"type": "doc", "content": [{"type": "image", "attrs": {"uploadId": upload_id, "alt": "Blue square", "caption": "A sample image"}}]}
        saved = posts.save_edit(edit["id"], "With image", image_body, edit["version"])
        self.assertFalse(media.public_upload(upload_id))
        published = posts.publish_edit(edit["id"], saved["version"])
        self.assertTrue(media.public_upload(upload_id))
        changed = posts.save_edit(edit["id"], "Image removed in working copy", body("No image"), published["version"])
        self.assertTrue(media.public_upload(upload_id))
        posts.publish_edit(edit["id"], changed["version"])
        self.assertFalse(media.public_upload(upload_id))

    def test_missing_images_prevent_publication_and_invalid_ids_are_rejected(self):
        edit = posts.new_draft()
        saved = posts.save_edit(edit["id"], "Missing image", {"type": "doc", "content": [
            {"type": "image", "attrs": {"uploadId": "a" * 32, "alt": "", "caption": ""}},
        ]}, edit["version"])
        with self.assertRaises(ValueError):
            posts.publish_edit(edit["id"], saved["version"])
        self.assertIsNone(posts.latest_published())
        for invalid in ("../app.py", "a" * 31, "/etc/passwd", None):
            self.assertIsNone(media.get_upload(invalid))
            self.assertFalse(media.public_upload(invalid))

    def test_image_type_size_animation_and_metadata_are_checked(self):
        for raw in (b'<svg xmlns="http://www.w3.org/2000/svg"></svg>', b"not an image", b"x" * (media.MAX_UPLOAD_BYTES + 1)):
            with self.assertRaises(ValueError):
                media.save_upload(FileStorage(BytesIO(raw), filename="fake.png", content_type="image/png"))
        with self.assertRaises(ValueError):
            media.save_upload(image_file("GIF"))
        original_limit = media.MAX_IMAGE_PIXELS
        try:
            media.MAX_IMAGE_PIXELS = 100
            with self.assertRaises(ValueError):
                media.save_upload(image_file())
        finally:
            media.MAX_IMAGE_PIXELS = original_limit
        exif = Image.Exif()
        exif[0x010E] = "Private metadata"
        upload = media.save_upload(image_file("JPEG", exif=exif))
        with Image.open(upload["path"]) as clean:
            self.assertEqual(dict(clean.getexif()), {})


if __name__ == "__main__":
    unittest.main()
