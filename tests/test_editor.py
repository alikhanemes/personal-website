"""Private editor boundaries and publication workflow, using isolated local data."""

from io import BytesIO
import json
from pathlib import Path
import re
import shutil
import unittest
from uuid import uuid4

from PIL import Image
from werkzeug.security import check_password_hash, generate_password_hash

from app import create_app
from posts import get_db


PASSWORD = "Local editor test password 2026!"
PASSWORD_HASH = generate_password_hash(PASSWORD, method="scrypt")


def document(text="Private draft body.", upload_id=None):
    nodes = [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]
    if upload_id:
        nodes.append({"type": "image", "attrs": {
            "uploadId": upload_id, "alt": "An example image", "caption": "Example caption",
        }})
    return {"type": "doc", "content": nodes}


class EditorTests(unittest.TestCase):
    def setUp(self):
        self.data_dir = Path(__file__).resolve().parents[1] / ".qa" / "tests" / uuid4().hex
        self.data_dir.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.data_dir)
        self.app = create_app({
            "TESTING": True, "DATA_DIR": str(self.data_dir), "TELEGRAM_URL": "",
            "ADMIN_PASSWORD_HASH": PASSWORD_HASH,
        })
        self.client = self.app.test_client()
        self.runner = self.app.test_cli_runner()

    def csrf(self, client=None):
        response = (client or self.client).get("/shygarma")
        self.assertEqual(response.status_code, 200)
        match = re.search(r'<meta\s+name="csrf-token"\s+content="([^"]+)"', response.get_data(as_text=True))
        self.assertIsNotNone(match, "Sign-in shell needs an explicit CSRF token.")
        return match.group(1)

    def login(self, client=None, *, tab_id=None):
        client = client or self.client
        csrf = self.csrf(client)
        response = client.post("/shygarma/login", json={
            "password": PASSWORD, "tab_id": tab_id or str(uuid4()),
        }, headers={"X-CSRF-Token": csrf})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        result = response.get_json()
        return {"X-CSRF-Token": result.get("csrf", csrf), "X-Tab-Token": result["tabToken"]}

    def post_value(self, response, expected=200):
        self.assertEqual(response.status_code, expected, response.get_data(as_text=True))
        payload = response.get_json()
        return payload.get("post", payload)

    def create(self, headers, title="Private draft title", body=None):
        response = self.client.post("/shygarma/api/posts", json={
            "title": title, "body": body or document(),
        }, headers=headers)
        self.assertIn(response.status_code, (200, 201), response.get_data(as_text=True))
        payload = response.get_json()
        return payload.get("post", payload)

    def save(self, post, headers, *, title=None, body=None, version=None):
        return self.client.put(f"/shygarma/api/posts/{post['id']}", json={
            "title": title or post["title"], "body": body or post["body"],
            "version": post["version"] if version is None else version,
        }, headers=headers)

    def publish(self, post, headers):
        return self.client.post(f"/shygarma/api/posts/{post['id']}/publish", json={
            "version": post["version"],
        }, headers=headers)

    def upload(self, headers):
        buffer = BytesIO()
        Image.new("RGB", (40, 24), color="white").save(buffer, format="PNG")
        buffer.seek(0)
        response = self.client.post("/shygarma/api/uploads", data={
            "file": (buffer, "example.png", "image/png"),
        }, headers=headers)
        self.assertIn(response.status_code, (200, 201), response.get_data(as_text=True))
        payload = response.get_json()
        return payload.get("upload", payload)

    def test_public_pages_do_not_offer_editor_or_load_editor_javascript(self):
        for path in ("/", "/posts"):
            with self.subTest(path=path):
                markup = self.client.get(path).get_data(as_text=True)
                self.assertNotIn("/shygarma", markup)
                self.assertNotIn("tiptap", markup.lower())
                self.assertNotIn("editor.js", markup)
        for path in ("/shygarma/assets/editor.js", "/shygarma/assets/editor.css"):
            self.assertIn(self.client.get(path).status_code, (401, 403, 404))
        for path in ("/static/editor.js", "/static/editor.css"):
            self.assertEqual(self.client.get(path).status_code, 404)

    def test_private_routes_require_session_and_current_tab_unlock(self):
        headers = self.login()
        post = self.create(headers)
        protected = ["/shygarma/status", "/shygarma/workspace", f"/shygarma/api/posts/{post['id']}"]
        for client, supplied in ((self.app.test_client(), {}), (self.client, {}),
                                 (self.client, {"X-Tab-Token": str(uuid4())})):
            for path in protected:
                with self.subTest(path=path, headers=bool(supplied)):
                    response = client.get(path, headers=supplied)
                    self.assertIn(response.status_code, (401, 403, 404))
                    self.assertNotIn(post["title"], response.get_data(as_text=True))
        self.assertEqual(self.client.get("/shygarma/status", headers=headers).status_code, 200)
        self.assertEqual(self.client.get("/posts/" + post["slug"]).status_code, 404)

    def test_csrf_is_required_for_login_and_every_mutation(self):
        self.csrf()
        attempt = self.client.post("/shygarma/login", json={"password": PASSWORD, "tab_id": str(uuid4())})
        self.assertEqual(attempt.status_code, 403)
        headers = self.login()
        post = self.create(headers)
        without_csrf = {"X-Tab-Token": headers["X-Tab-Token"]}
        requests = [
            ("post", "/shygarma/api/posts", {}),
            ("put", f"/shygarma/api/posts/{post['id']}", {"title": "Bad change", "body": document(), "version": post["version"]}),
            ("post", f"/shygarma/api/posts/{post['id']}/publish", {"version": post["version"]}),
            ("post", f"/shygarma/api/posts/{post['id']}/preview", {"version": post["version"]}),
            ("post", "/shygarma/logout", {}),
        ]
        for method, path, payload in requests:
            with self.subTest(path=path):
                response = getattr(self.client, method)(path, json=payload, headers=without_csrf)
                self.assertEqual(response.status_code, 403)
        cross_origin = dict(headers, Origin="https://unrelated.example")
        self.assertEqual(self.client.post("/shygarma/api/posts", json={}, headers=cross_origin).status_code, 403)
        upload = self.client.post("/shygarma/api/uploads", data={
            "file": (BytesIO(b"invalid"), "bad.png", "image/png"),
        }, headers=without_csrf)
        self.assertEqual(upload.status_code, 403)
        self.assertEqual(self.client.get("/shygarma/status", headers=headers).status_code, 200)

    def test_login_throttle_persists_across_anonymous_sessions(self):
        self.app.config.update(LOGIN_LIMIT=2, LOGIN_WINDOW=900)
        csrf = self.csrf()
        payload = {"password": "An incorrect password", "tab_id": str(uuid4())}
        for _ in range(2):
            response = self.client.post("/shygarma/login", json=payload, headers={"X-CSRF-Token": csrf})
            self.assertEqual(response.status_code, 401)
        other = self.app.test_client()
        response = other.post("/shygarma/login", json={"password": PASSWORD, "tab_id": str(uuid4())},
                              headers={"X-CSRF-Token": self.csrf(other)})
        self.assertEqual(response.status_code, 429)
        self.assertIn("Retry-After", response.headers)

    def test_cookie_is_httponly_and_logout_revokes_session(self):
        shell = self.client.get("/shygarma")
        cookie = shell.headers.get("Set-Cookie", "")
        self.assertIn("HttpOnly", cookie)
        self.assertRegex(cookie, r"SameSite=(?:Strict|Lax)")
        anonymous_token = self.client.get_cookie("blog_session").value
        headers = self.login()
        self.assertNotEqual(self.client.get_cookie("blog_session").value, anonymous_token)
        previous_session = self.app.test_client()
        previous_session.set_cookie("blog_session", anonymous_token)
        self.assertIn(previous_session.get("/shygarma/status", headers=headers).status_code, (401, 403))
        self.assertEqual(self.client.post("/shygarma/logout", json={}, headers=headers).status_code, 200)
        self.assertIn(self.client.get("/shygarma/status", headers=headers).status_code, (401, 403))

    def test_same_tab_navigation_ticket_is_short_lived_bound_and_one_use(self):
        headers = self.login()
        response = self.client.post("/shygarma/navigation", json={}, headers=headers)
        self.assertEqual(response.status_code, 200)
        ticket = response.get_json()["ticket"]
        wrong = self.client.post("/shygarma/resume", json={"ticket": ticket, "tab_id": str(uuid4())}, headers=headers)
        self.assertEqual(wrong.status_code, 401)
        payload = {"ticket": ticket, "tab_id": headers["X-Tab-Token"]}
        resumed = self.client.post("/shygarma/resume", json=payload, headers=headers)
        self.assertEqual(resumed.status_code, 200)
        self.assertEqual(self.client.post("/shygarma/resume", json=payload, headers=headers).status_code, 401)
        ticket = self.client.post("/shygarma/navigation", json={}, headers=headers).get_json()["ticket"]
        with self.app.app_context():
            database = get_db()
            database.execute("UPDATE navigation_tickets SET expires_at=0")
            database.commit()
        expired = self.client.post("/shygarma/resume", json={"ticket": ticket, "tab_id": headers["X-Tab-Token"]}, headers=headers)
        self.assertEqual(expired.status_code, 401)

    def test_expired_and_cli_revoked_sessions_cannot_read_drafts(self):
        headers = self.login()
        post = self.create(headers)
        with self.app.app_context():
            database = get_db()
            database.execute("UPDATE admin_sessions SET expires_at = 0")
            database.commit()
        self.assertIn(self.client.get(f"/shygarma/api/posts/{post['id']}", headers=headers).status_code, (401, 403))
        headers = self.login()
        result = self.runner.invoke(args=["revoke-sessions"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn(self.client.get("/shygarma/status", headers=headers).status_code, (401, 403))

    def test_hidden_admin_setup_stores_hash_and_revokes_existing_sessions(self):
        headers = self.login()
        password = "Another private setup password!"
        result = self.runner.invoke(args=["set-admin"], input=password + "\n" + password + "\n")
        self.assertEqual(result.exit_code, 0, result.output + repr(result.exception))
        self.assertNotIn(password, result.output)
        settings = json.loads((self.data_dir / "admin.json").read_text(encoding="utf-8"))
        self.assertNotEqual(settings["password_hash"], password)
        self.assertTrue(check_password_hash(settings["password_hash"], password))
        self.assertIn(self.client.get("/shygarma/status", headers=headers).status_code, (401, 403))

    def test_unconfigured_admin_cannot_sign_in_and_setup_rejects_short_password(self):
        self.app.config["ADMIN_PASSWORD_HASH"] = None
        csrf = self.csrf()
        response = self.client.post("/shygarma/login", json={"password": PASSWORD, "tab_id": str(uuid4())},
                                    headers={"X-CSRF-Token": csrf})
        self.assertEqual(response.status_code, 503)
        short = self.runner.invoke(args=["set-admin"], input="short\nshort\n")
        self.assertNotEqual(short.exit_code, 0)
        self.assertFalse((self.data_dir / "admin.json").exists())

    def test_draft_resume_preview_publish_and_explicit_update(self):
        headers = self.login()
        seed = self.runner.invoke(args=["seed"])
        self.assertEqual(seed.exit_code, 0, seed.output)
        post = self.create(headers)
        post = self.post_value(self.save(post, headers, body=document("A full new article ending.")))
        reopened = self.post_value(self.client.get(f"/shygarma/api/posts/{post['id']}", headers=headers))
        self.assertEqual(reopened["body"], post["body"])
        self.assertEqual(reopened["version"], post["version"])
        preview = self.client.post(f"/shygarma/api/posts/{post['id']}/preview", json={"version": post["version"]}, headers=headers)
        self.assertEqual(preview.status_code, 200)
        self.assertIn("A full new article ending.", preview.get_json()["html"])
        self.assertNotIn(post["title"], self.client.get("/").get_data(as_text=True))
        published = self.post_value(self.publish(post, headers))
        for path in ("/", "/posts/" + published["slug"]):
            self.assertIn("A full new article ending.", self.client.get(path).get_data(as_text=True))
        archive = self.client.get("/posts").get_data(as_text=True)
        self.assertIn("/posts/sample-a-place-for-longer-thoughts", archive)
        staged = self.post_value(self.save(published, headers, title="Explicitly updated title", body=document("Updated only after Update.")))
        permalink = "/posts/" + published["slug"]
        self.assertNotIn("Updated only after Update.", self.client.get(permalink).get_data(as_text=True))
        updated = self.post_value(self.publish(staged, headers))
        self.assertEqual(updated["slug"], published["slug"])
        self.assertEqual(updated["published_at"], published["published_at"])
        self.assertIn("Updated only after Update.", self.client.get(permalink).get_data(as_text=True))

    def test_stale_autosave_and_publish_cannot_overwrite_newer_work(self):
        headers = self.login()
        initial = self.create(headers)
        newer = self.post_value(self.save(initial, headers, body=document("Newest accepted words.")))
        stale = self.save(initial, headers, body=document("Old delayed request."))
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(self.publish(initial, headers).status_code, 409)
        current = self.post_value(self.client.get(f"/shygarma/api/posts/{initial['id']}", headers=headers))
        self.assertEqual(current["body"], newer["body"])
        self.assertEqual(current["version"], newer["version"])
        self.assertEqual(self.client.get("/posts/" + initial["slug"]).status_code, 404)

    def test_image_validation_privacy_and_publication_reference(self):
        headers = self.login()
        upload = self.upload(headers)
        private_url = "/shygarma/api/media/" + str(upload["id"])
        public_url = "/media/" + str(upload["id"])
        with self.client.get(private_url, headers=headers) as private_image:
            self.assertEqual(private_image.status_code, 200)
        self.assertIn(self.client.get(private_url).status_code, (401, 403, 404))
        anonymous = self.app.test_client()
        self.assertEqual(anonymous.get(public_url).status_code, 404)
        for content, name, mime in ((b"<svg><script>alert(1)</script></svg>", "unsafe.svg", "image/svg+xml"),
                                    (b"plain text", "pretend.png", "image/png")):
            with self.subTest(name=name):
                bad = self.client.post("/shygarma/api/uploads", data={"file": (BytesIO(content), name, mime)}, headers=headers)
                self.assertIn(bad.status_code, (400, 413, 415))
        post = self.create(headers, body=document("A draft image stays private.", upload["id"]))
        self.assertEqual(anonymous.get(public_url).status_code, 404)
        preview = self.client.post(f"/shygarma/api/posts/{post['id']}/preview",
                                   json={"version": post["version"]}, headers=headers)
        self.assertEqual(preview.status_code, 200)
        # The editor replaces these media references with authorized fetch/blob
        # URLs before displaying srcdoc; the HTML itself carries no capability.
        self.assertIn(public_url, preview.get_json()["html"])
        self.assertIn("Example caption", preview.get_json()["html"])
        self.assertNotIn(headers["X-Tab-Token"], preview.get_json()["html"])
        self.assertEqual(anonymous.get(public_url).status_code, 404)
        published = self.post_value(self.publish(post, headers))
        markup = anonymous.get("/posts/" + published["slug"]).get_data(as_text=True)
        self.assertIn(public_url, markup)
        self.assertIn("Example caption", markup)
        with anonymous.get(public_url) as image:
            self.assertEqual(image.status_code, 200)
            self.assertEqual(image.mimetype, "image/png")
            self.assertEqual(image.headers.get("X-Content-Type-Options"), "nosniff")


if __name__ == "__main__":
    unittest.main()
