"""Production holds, HTTPS cookies, uncached withdrawal and disk persistence."""

from io import BytesIO
import os
from pathlib import Path
import re
import shutil
import unittest
from unittest.mock import patch
from uuid import uuid4

from PIL import Image
from werkzeug.datastructures import FileStorage
from werkzeug.security import generate_password_hash

from app import create_app
import media
import operations
import posts
import runtime


PASSWORD = "Runtime isolated verification password!"
PASSWORD_HASH = generate_password_hash(PASSWORD, method="scrypt")


def document(text="Runtime article", upload_id=None):
    content = [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]
    if upload_id:
        content.append({"type": "image", "attrs": {"uploadId": upload_id, "alt": "Test image", "caption": "Caption"}})
    return {"type": "doc", "content": content}


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(__file__).resolve().parents[1] / ".qa" / "tests" / uuid4().hex
        self.directory.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.directory)
        self.app = self.application()

    def application(self, **config):
        return create_app({"TESTING": True, "DATA_DIR": str(self.directory),
                           "ADMIN_PASSWORD_HASH": PASSWORD_HASH, "PUBLIC_ENABLED": True, **config})

    def article(self, app=None, *, publish=True):
        app = app or self.app
        with app.app_context():
            stream = BytesIO()
            Image.new("RGB", (13, 9), "blue").save(stream, format="PNG")
            stream.seek(0)
            upload = media.save_upload(FileStorage(stream, filename="runtime.png"))
            draft = posts.new_draft("Private runtime title")
            draft = posts.save_edit(draft["id"], draft["title"], document("Complete runtime body", upload["id"]), draft["version"])
            if publish:
                draft = posts.publish_edit(draft["id"], draft["version"])
            return draft, upload

    def assert_held(self, client, post, upload):
        for path in ("/", "/posts", "/posts/" + post["slug"], "/media/" + upload["id"],
                     "/shygarma", "/shygarma/api/posts/" + str(post["id"]), "/subscribe", "/static/style.css"):
            with self.subTest(path=path):
                response = client.get(path)
                self.assertEqual(response.status_code, 503)
                self.assertIn("no-store", response.headers.get("Cache-Control", ""))
                self.assertNotIn(post["title"], response.get_data(as_text=True))
                self.assertNotIn("Complete runtime body", response.get_data(as_text=True))
        self.assertEqual(client.get("/healthz").status_code, 200)
        self.assertEqual(client.get("/healthz").get_json(), {"status": "ok"})

    def test_public_disabled_holds_all_routes_except_health_without_exposing_articles(self):
        post, upload = self.article()
        self.app.config["PUBLIC_ENABLED"] = False
        self.assert_held(self.app.test_client(), post, upload)

    def test_restore_marker_holds_articles_media_editor_and_subscriptions(self):
        post, upload = self.article()
        marker = self.directory / operations.MARKER
        marker.write_text('{"state":"restoring"}', encoding="utf-8")
        self.assert_held(self.app.test_client(), post, upload)
        marker.unlink()
        self.assertEqual(self.app.test_client().get("/posts/" + post["slug"]).status_code, 200)

    def test_production_environment_defaults_to_closed_without_explicit_release(self):
        with patch.dict(os.environ, {"BLOG_ENV": "production"}, clear=True):
            app = create_app({"TESTING": True, "DATA_DIR": str(self.directory)})
        self.assertTrue(app.config["PRODUCTION"])
        self.assertFalse(app.config["PUBLIC_ENABLED"])
        self.assertEqual(app.test_client().get("/").status_code, 503)
        self.assertEqual(app.test_client().get("/healthz").status_code, 200)

    def test_production_uses_secure_cookie_and_trusts_one_https_proxy(self):
        app = self.application(PRODUCTION=True, COOKIE_SECURE=False)
        self.assertTrue(app.config["COOKIE_SECURE"])
        client = app.test_client()
        forwarded = {"X-Forwarded-Proto": "https", "X-Forwarded-For": "203.0.113.10"}
        response = client.get("/shygarma", base_url="http://alikhan-zarubay.com", headers=forwarded)
        self.assertEqual(response.status_code, 200)
        cookie = response.headers.get("Set-Cookie", "")
        self.assertIn("Secure", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        csrf = re.search(r'<meta name="csrf-token" content="([^"]+)"', response.get_data(as_text=True)).group(1)
        response = client.post("/shygarma/login", base_url="http://alikhan-zarubay.com",
                               json={"password": PASSWORD, "tab_id": uuid4().hex},
                               headers={**forwarded, "Origin": "https://alikhan-zarubay.com", "X-CSRF-Token": csrf})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertIn("Secure", response.headers.get("Set-Cookie", ""))

    def test_production_samples_hold_http_and_background_delivery(self):
        app = self.application(PRODUCTION=True)
        result = app.test_cli_runner().invoke(args=["seed"])
        self.assertEqual(result.exit_code, 0, result.output)
        client = app.test_client()
        for path in ("/", "/posts", "/posts/sample-a-place-for-longer-thoughts", "/shygarma", "/subscribe"):
            self.assertEqual(client.get(path).status_code, 503)
        self.assertEqual(client.get("/healthz").status_code, 200)
        with patch("runtime.newsletter.process_outbox") as sends, patch("runtime.media.cleanup_media") as cleanup:
            runtime.run_once(app)
        sends.assert_not_called()
        cleanup.assert_not_called()

    def test_real_writing_preserves_urls_even_if_a_slug_starts_with_sample(self):
        app = self.application(PRODUCTION=True)
        with app.app_context():
            for slug in ('sample-size-in-statistics', 'sample-a-place-for-longer-thoughts'):
                posts.create_post(slug, 'Real writing at a stable URL', document('Original real article text.'), '2026-01-01T00:00:00Z')
            self.assertFalse(operations.production_samples_present())
        client = app.test_client()
        for slug in ('sample-size-in-statistics', 'sample-a-place-for-longer-thoughts'):
            self.assertEqual(client.get('/posts/' + slug).status_code, 200)

    def test_dynamic_responses_and_withdrawn_media_never_use_public_cache(self):
        post, upload = self.article()
        client = self.app.test_client()
        permalink, image_url = "/posts/" + post["slug"], "/media/" + upload["id"]
        for path in ("/", "/posts", permalink, "/subscribe", "/posts/missing", "/media/" + "0" * 32, "/shygarma"):
            response = client.get(path)
            self.assertIn("no-store", response.headers.get("Cache-Control", ""))
        image = client.get(image_url)
        try:
            self.assertEqual(image.status_code, 200)
            self.assertIn("no-store", image.headers.get("Cache-Control", ""))
            etag = image.headers.get("ETag", '"old-image"')
        finally:
            image.close()
        with self.app.app_context():
            archived = posts.archive_post(post["id"], post["version"])
        for path in (permalink, image_url):
            response = client.get(path, headers={"If-None-Match": etag})
            self.assertEqual(response.status_code, 404)
            self.assertIn("no-store", response.headers.get("Cache-Control", ""))
            self.assertNotIn(post["title"], response.get_data(as_text=True))
        with self.app.app_context():
            posts.permanently_delete_post(post["id"], "DELETE", archived["version"])
        for path in (permalink, image_url):
            response = client.get(path)
            self.assertEqual(response.status_code, 404)
            self.assertIn("no-store", response.headers.get("Cache-Control", ""))

    def test_restart_retains_draft_working_copy_and_image_without_exposing_either(self):
        draft, upload = self.article(publish=False)
        restarted = self.application()
        with restarted.app_context():
            reopened = posts.get_edit(draft["id"])
            stored_image = media.get_upload(upload["id"])
            self.assertEqual(reopened["slug"], draft["slug"])
            self.assertEqual(reopened["body"], draft["body"])
            self.assertEqual(reopened["version"], draft["version"])
            self.assertEqual(stored_image["path"], upload["path"])
            self.assertTrue(Path(stored_image["path"]).is_file())
        client = restarted.test_client()
        self.assertEqual(client.get("/posts/" + draft["slug"]).status_code, 404)
        self.assertEqual(client.get("/media/" + upload["id"]).status_code, 404)
        self.assertEqual(client.get("/shygarma/api/posts/" + str(draft["id"])).status_code, 401)

    def test_worker_does_not_send_or_clean_during_disabled_or_restore_hold(self):
        for hold in ("disabled", "restore"):
            with self.subTest(hold=hold):
                self.app.config["PUBLIC_ENABLED"] = hold != "disabled"
                if hold == "restore":
                    (self.directory / operations.MARKER).write_text('{}', encoding="utf-8")
                with patch("runtime.newsletter.process_outbox") as sends, patch("runtime.media.cleanup_media") as cleanup:
                    runtime.run_once(self.app)
                sends.assert_not_called()
                cleanup.assert_not_called()

    def test_media_deleted_during_request_returns_normal_uncached_404(self):
        _, upload = self.article()
        client = self.app.test_client()
        with patch("app.media.public_upload", return_value=True), patch("app.media.get_upload", return_value=None):
            response = client.get("/media/" + upload["id"])
            self.assertEqual(response.status_code, 404)
            self.assertIn("no-store", response.headers.get("Cache-Control", ""))
        with patch("app.send_file", side_effect=FileNotFoundError("Concurrent file cleanup")):
            response = client.get("/media/" + upload["id"])
            self.assertEqual(response.status_code, 404)
            self.assertIn("no-store", response.headers.get("Cache-Control", ""))


if __name__ == "__main__":
    unittest.main()
