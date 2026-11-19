"""Private backup verification and restore publication safeguards."""

from io import BytesIO
import json
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
from uuid import uuid4

from flask import Flask
from PIL import Image
from werkzeug.datastructures import FileStorage

import auth
import media
import newsletter
import operations
import posts


def document(text="Article", upload_id=None):
    nodes = [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]
    if upload_id:
        nodes.append({"type": "image", "attrs": {"uploadId": upload_id, "alt": "Test", "caption": "Test"}})
    return {"type": "doc", "content": nodes}


class OperationTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parents[1] / ".qa" / "tests" / uuid4().hex
        self.data = self.root / "data"
        self.data.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.root)
        self.app = Flask(__name__)
        self.app.config.update(DATA_DIR=str(self.data), DATABASE=str(self.data / "blog.sqlite3"))
        posts.init_app(self.app)
        operations.init_app(self.app)
        context = self.app.app_context()
        context.push()
        self.addCleanup(context.pop)
        posts.init_db()
        auth.init_schema()
        newsletter.init_schema()

    def upload(self):
        stream = BytesIO()
        Image.new("RGB", (10, 8), "white").save(stream, format="PNG")
        stream.seek(0)
        return media.save_upload(FileStorage(stream, filename="image.png"))

    def published(self, title="Real article", upload_id=None):
        draft = posts.new_draft(title)
        saved = posts.save_edit(draft["id"], title, document(title, upload_id), draft["version"])
        return posts.publish_edit(saved["id"], saved["version"])

    def backup(self, name="backup"):
        destination = self.root / name
        result = operations.backup_data(destination)
        self.assertTrue(result["verified"])
        return destination

    def test_backup_contains_verified_private_database_media_and_not_credentials(self):
        image = self.upload()
        published = self.published(upload_id=image["id"])
        posts.save_edit(published["id"], "Working version", document("Private edits"), published["version"])
        (self.data / "admin.json").write_text('{"password_hash":"local secret"}', encoding="utf-8")
        (self.data / ".env").write_text("RESEND_API_KEY=do-not-copy", encoding="utf-8")
        destination = self.backup()
        _, manifest, summary, _ = operations.verify_backup(destination)
        self.assertEqual(summary["posts"], 1)
        self.assertEqual(summary["images"], 1)
        self.assertIn("uploads/" + image["filename"], manifest["files"])
        self.assertFalse((destination / "admin.json").exists())
        self.assertFalse((destination / ".env").exists())
        self.assertTrue(all(len(digest) == 64 for digest in manifest["files"].values()))
        for path in (destination, self.data / "nested", Path(self.app.static_folder) / "backup"):
            with self.assertRaises(ValueError):
                operations.backup_data(path)

    def test_tampered_or_unsafe_backup_does_not_change_live_database(self):
        published = self.published()
        destination = self.backup()
        with (destination / "blog.sqlite3").open("ab") as file:
            file.write(b"changed")
        with self.assertRaises(ValueError):
            operations.restore_data(destination)
        self.assertEqual(posts.latest_published()["slug"], published["slug"])
        self.assertFalse((self.data / operations.MARKER).exists())
        destination = self.backup("unsafe")
        manifest_path = destination / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["files"]["../outside.sqlite3"] = "0" * 64
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaises(ValueError):
            operations.restore_data(destination)

    def test_restore_stays_held_retains_working_copy_revokes_sessions_skips_unsent_email(self):
        upload = self.upload()
        post = self.published(upload_id=upload["id"])
        saved = posts.save_edit(post["id"], "Private working title", document("Private work", upload["id"]), post["version"])
        db = posts.get_db()
        db.execute("INSERT INTO admin_sessions VALUES ('session','csrf',9999999999,1)")
        db.execute("INSERT INTO tab_unlocks VALUES ('session','tab')")
        db.execute("INSERT INTO navigation_tickets VALUES ('ticket','session','tab',9999999999)")
        db.execute("INSERT INTO subscriptions VALUES (1,'test@example.com',1,1,NULL)")
        db.execute("""INSERT INTO email_jobs(id,post_id,subscription_id,kind,status,payload,idempotency_key,created_at,available_at)
                      VALUES('job',?,1,'post','retry','{}','key',1,1)""", (post["id"],))
        db.commit()
        destination = self.backup()
        posts.save_edit(post["id"], "Later working title", document("Later"), saved["version"])
        result = operations.restore_data(destination)
        self.assertTrue(result["held"])
        self.assertEqual(posts.get_edit(post["id"])["title"], "Private working title")
        self.assertTrue(media.public_upload(upload["id"]))
        self.assertEqual(posts.get_db().execute("SELECT COUNT(*) FROM admin_sessions").fetchone()[0], 0)
        self.assertEqual(posts.get_db().execute("SELECT COUNT(*) FROM tab_unlocks").fetchone()[0], 0)
        self.assertEqual(posts.get_db().execute("SELECT COUNT(*) FROM navigation_tickets").fetchone()[0], 0)
        self.assertEqual(posts.get_db().execute("SELECT status FROM email_jobs").fetchone()[0], "skipped")
        self.assertTrue(operations.review_restored_data()["held"])
        result = operations.review_restored_data(approve=True)
        self.assertTrue(result["approved"])
        self.assertFalse(result["held"])

    def test_restore_merges_newer_deletions_and_removes_resurrected_post_and_images(self):
        upload = self.upload()
        removed = self.published("Must remain deleted", upload["id"])
        kept = self.published("Keep this article")
        destination = self.backup()
        posts.permanently_delete_post(removed["id"], "DELETE", removed["version"])
        self.assertFalse(Path(upload["path"]).exists())
        result = operations.restore_data(destination)
        self.assertEqual(result["removed_deleted_posts"], 1)
        self.assertIsNone(posts.published_by_slug(removed["slug"]))
        self.assertIsNotNone(posts.published_by_slug(kept["slug"]))
        self.assertIsNone(media.get_upload(upload["id"]))
        self.assertFalse(Path(upload["path"]).exists())
        self.assertTrue(operations.review_restored_data(approve=True)["approved"])

    def test_external_ledger_protects_deleted_posts_after_an_older_disk_snapshot(self):
        removed = self.published("Removed after snapshot")
        destination = self.backup()
        external = self.root / "separate-deletions.json"
        external.write_text(json.dumps({"version": 1, "deleted_posts": [
            {"slug": removed["slug"], "deleted_at": "2026-11-19T01:00:00Z"}
        ]}), encoding="utf-8")
        result = operations.restore_data(destination, deletions_path=external)
        self.assertEqual(result["removed_deleted_posts"], 1)
        self.assertIsNone(posts.get_edit(removed["id"]))

    def test_review_cannot_release_samples_in_production_or_missing_media(self):
        post = self.published("Sample article: demonstration")
        self.backup()
        marker = self.data / operations.MARKER
        marker.write_text('{"state":"review_required"}', encoding="utf-8")
        self.app.config["PRODUCTION"] = True
        with self.assertRaisesRegex(ValueError, "sample posts"):
            operations.review_restored_data(approve=True)
        self.assertTrue(marker.exists())
        post = posts.archive_post(post["id"], post["version"])
        image = self.upload()
        self.published("Real image article", image["id"])
        Path(image["path"]).unlink()
        with self.assertRaisesRegex(ValueError, "missing"):
            operations.review_restored_data(approve=True)
        self.assertTrue(marker.exists())

    def test_cli_reports_hold_and_requires_explicit_approval(self):
        self.published()
        runner = self.app.test_cli_runner()
        destination = self.root / "cli-backup"
        result = runner.invoke(args=["backup-data", "--output", str(destination)])
        self.assertEqual(result.exit_code, 0, result.output + repr(result.exception))
        result = runner.invoke(args=["restore-data", "--source", str(destination)])
        self.assertEqual(result.exit_code, 0, result.output + repr(result.exception))
        self.assertTrue(json.loads(result.output)["held"])
        result = runner.invoke(args=["review-restored-data"])
        self.assertTrue(json.loads(result.output)["held"])
        result = runner.invoke(args=["review-restored-data", "--approve"])
        self.assertEqual(result.exit_code, 0, result.output + repr(result.exception))
        self.assertFalse(json.loads(result.output)["held"])

    def test_interrupted_restore_stays_held_until_successful_retry_and_review(self):
        image = self.upload()
        self.published(upload_id=image["id"])
        destination = self.backup()
        with patch.object(shutil, "copyfile", side_effect=PermissionError("Disk temporarily unavailable")):
            with self.assertRaises(PermissionError):
                operations.restore_data(destination)
        marker = self.data / operations.MARKER
        self.assertTrue(marker.exists())
        self.assertEqual(json.loads(marker.read_text(encoding="utf-8"))["state"], "restoring")
        with self.assertRaisesRegex(ValueError, "did not finish"):
            operations.review_restored_data(approve=True)
        self.assertTrue(operations.restore_data(destination)["held"])
        self.assertFalse(operations.review_restored_data(approve=True)["held"])

    def test_deleted_post_stays_deleted_if_sidecar_write_fails_and_reports_warning(self):
        post = self.published()
        with patch.object(operations, "_atomic_json", side_effect=PermissionError("Ledger is locked")):
            result = posts.permanently_delete_post(post["id"], "DELETE", post["version"])
        self.assertTrue(result["deleted"])
        self.assertFalse(result["deletion_ledger"]["saved"])
        self.assertIn("locked", result["deletion_ledger"]["error"])
        self.assertIsNone(posts.get_edit(post["id"]))
        self.assertIn(post["slug"], operations._db_deletions(posts.get_db()))
        self.assertTrue(operations.persist_deletion_ledger()["saved"])


if __name__ == "__main__":
    unittest.main()
