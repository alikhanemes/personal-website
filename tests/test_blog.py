"""Public behavior checks. Run with: python -m unittest discover -s tests -v."""

from html.parser import HTMLParser
import json
from pathlib import Path
import shutil
import unittest
from uuid import uuid4

from app import create_app
from posts import create_post, get_db


LATEST_SLUG = "sample-a-place-for-longer-thoughts"
OLDER_SLUG = "sample-keeping-a-notebook"
DRAFT_SLUG = "sample-unpublished-draft"


class Document(HTMLParser):
    """Small HTML inspector, avoiding a separate test-only dependency."""

    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.elements = []
        self.stack = []
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        element = {"tag": tag, "attrs": dict(attrs), "text": ""}
        self.elements.append(element)
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self.stack.append(element)

    def handle_startendtag(self, tag, attrs):
        self.elements.append({"tag": tag, "attrs": dict(attrs), "text": ""})

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index]["tag"] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        for element in self.stack:
            element["text"] += data

    @property
    def links(self):
        return [item["attrs"].get("href", "") for item in self.elements if item["tag"] == "a"]

    def disabled_control(self, label):
        for element in self.elements:
            attrs = element["attrs"]
            name = " ".join((element["text"], attrs.get("aria-label", ""), attrs.get("title", "")))
            if label.lower() in name.lower() and ("disabled" in attrs or attrs.get("aria-disabled") == "true"):
                return True
        return False


def body(text):
    return json.dumps({"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]})


class BlogTests(unittest.TestCase):
    def setUp(self):
        # mkdir's inherited permissions also work in restricted Windows sessions;
        # Python 3.13's private TemporaryDirectory ACL can prevent SQLite access.
        self.data_dir = Path(__file__).resolve().parents[1] / ".qa" / "tests" / uuid4().hex
        self.data_dir.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.data_dir)
        self.app = create_app({"TESTING": True, "DATA_DIR": str(self.data_dir), "TELEGRAM_URL": ""})
        self.client = self.app.test_client()
        self.runner = self.app.test_cli_runner()

    def seed(self):
        result = self.runner.invoke(args=["seed"])
        self.assertEqual(result.exit_code, 0, result.output + repr(result.exception))

    def rows(self):
        with self.app.app_context():
            return [dict(row) for row in get_db().execute("SELECT * FROM posts ORDER BY id")]

    def insert(self, slug, title, text, published_at):
        with self.app.app_context():
            database = get_db()
            database.execute(
                "INSERT INTO posts (slug, title, body, published_at) VALUES (?, ?, ?, ?)",
                (slug, title, body(text), published_at),
            )
            database.commit()

    def test_empty_database_and_missing_articles(self):
        for path in ("/", "/posts"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertNotIn("Traceback", response.get_data(as_text=True))
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.client.get("/posts/missing-article").status_code, 404)

    def test_seed_is_repeatable_and_preserves_existing_content(self):
        init = self.runner.invoke(args=["init-db"])
        self.assertEqual(init.exit_code, 0, init.output)
        self.seed()
        rows = self.rows()
        self.assertEqual(len(rows), 3)
        self.assertEqual({row["slug"] for row in rows}, {LATEST_SLUG, OLDER_SLUG, DRAFT_SLUG})
        self.assertEqual(sum(row["published_at"] is not None for row in rows), 2)
        with self.app.app_context():
            database = get_db()
            database.execute("UPDATE posts SET title = ?, body = ? WHERE slug = ?", ("Edited sample", body("An edit to preserve."), LATEST_SLUG))
            database.commit()
        self.seed()
        init_again = self.runner.invoke(args=["init-db"])
        self.assertEqual(init_again.exit_code, 0, init_again.output)
        after = self.rows()
        self.assertEqual(len(after), 3)
        edited = next(row for row in after if row["slug"] == LATEST_SLUG)
        self.assertEqual(edited["title"], "Edited sample")
        self.assertIn("An edit to preserve.", edited["body"])

    def test_main_archive_and_permanent_articles(self):
        self.seed()
        records = {row["slug"]: row for row in self.rows()}
        main = self.client.get("/")
        self.assertEqual(main.status_code, 200)
        markup = main.get_data(as_text=True)
        self.assertIn(records[LATEST_SLUG]["title"], markup)
        self.assertIn("This is where the next article will begin.", markup)
        self.assertIn("/posts/" + LATEST_SLUG, Document(markup).links)
        self.assertNotIn(records[DRAFT_SLUG]["title"], markup)

        archive = self.client.get("/posts")
        self.assertEqual(archive.status_code, 200)
        markup = archive.get_data(as_text=True)
        self.assertIn(records[OLDER_SLUG]["title"], markup)
        self.assertIn("/posts/" + OLDER_SLUG, Document(markup).links)
        self.assertNotIn(records[LATEST_SLUG]["title"], markup)
        self.assertNotIn(records[DRAFT_SLUG]["title"], markup)

        for slug in (LATEST_SLUG, OLDER_SLUG):
            with self.subTest(slug=slug):
                page = self.client.get("/posts/" + slug)
                self.assertEqual(page.status_code, 200)
                self.assertIn(records[slug]["title"], page.get_data(as_text=True))
        self.assertEqual(self.client.get("/posts/" + DRAFT_SLUG).status_code, 404)

    def test_publication_order_and_deterministic_ties(self):
        self.insert("oldest", "Oldest title", "Oldest body.", "2025-01-01T00:00:00+00:00")
        self.insert("tie-first", "First tied title", "First tied body.", "2026-01-01T00:00:00+00:00")
        self.insert("tie-second", "Second tied title", "Second tied body.", "2026-01-01T00:00:00+00:00")
        self.insert("private-draft", "Private draft title", "Private draft body.", None)
        main = self.client.get("/").get_data(as_text=True)
        self.assertIn("Second tied title", main)
        self.assertIn("Second tied body.", main)
        self.assertNotIn("Private draft title", main)
        archive = self.client.get("/posts").get_data(as_text=True)
        self.assertLess(archive.index("First tied title"), archive.index("Oldest title"))
        self.assertNotIn("Second tied title", archive)
        self.assertNotIn("Private draft title", archive)
        self.assertEqual(self.client.get("/posts/private-draft").status_code, 404)

    def test_publication_precision_and_timezone_normalization(self):
        document = json.loads(body("A complete article body."))
        with self.app.app_context():
            # The newest post has the lowest ID; submillisecond precision must
            # take precedence over the ID used to break exact timestamp ties.
            create_post("precise-newest", "Precisely newest title", document, "2026-01-01T00:00:00.000200Z")
            create_post("precise-older", "Precisely older title", document, "2026-01-01T00:00:00.000100Z")
            create_post("offset-equivalent", "Offset equivalent title", document, "2026-01-01T05:00:00.000100+05:00")
            with self.assertRaises(ValueError):
                create_post("naive-timestamp", "Naive timestamp title", document, "2026-01-01T00:00:00.000300")
        records = {row["slug"]: row for row in self.rows()}
        self.assertEqual(len(records), 3)
        self.assertEqual(records["precise-newest"]["published_at"], "2026-01-01T00:00:00.000200Z")
        self.assertEqual(records["precise-older"]["published_at"], "2026-01-01T00:00:00.000100Z")
        self.assertEqual(records["offset-equivalent"]["published_at"], "2026-01-01T00:00:00.000100Z")
        main = self.client.get("/").get_data(as_text=True)
        self.assertIn("Precisely newest title", main)
        self.assertIn("A complete article body.", main)
        archive = self.client.get("/posts").get_data(as_text=True)
        self.assertNotIn("Precisely newest title", archive)
        self.assertLess(archive.index("Offset equivalent title"), archive.index("Precisely older title"))

    def test_header_links_to_subscription_and_telegram_is_configurable(self):
        document = Document(self.client.get("/").get_data(as_text=True))
        self.assertIn("/", document.links)
        self.assertIn("/posts", document.links)
        self.assertIn("/subscribe", document.links)
        self.assertFalse(document.disabled_control("Subscribe by email"))
        self.assertTrue(document.disabled_control("Telegram"))
        self.assertFalse(any(link.startswith("mailto:") for link in document.links))

        configured_app = create_app({"TESTING": True, "DATA_DIR": str(self.data_dir), "TELEGRAM_URL": "https://t.me/example_blog"})
        configured = Document(configured_app.test_client().get("/").get_data(as_text=True))
        self.assertIn("https://t.me/example_blog", configured.links)
        unsafe_app = create_app({"TESTING": True, "DATA_DIR": str(self.data_dir), "TELEGRAM_URL": "javascript:alert('unsafe')"})
        unsafe = Document(unsafe_app.test_client().get("/").get_data(as_text=True))
        self.assertTrue(unsafe.disabled_control("Telegram"))
        self.assertFalse(any("javascript:" in link.lower() for link in unsafe.links))

    def test_article_text_and_titles_are_escaped(self):
        self.insert("escaped", "Title <script>title()</script>", "Body <script>alert(42)</script>.", "2026-01-01T00:00:00+00:00")
        for path in ("/", "/posts/escaped"):
            with self.subTest(path=path):
                markup = self.client.get(path).get_data(as_text=True)
                self.assertNotIn("<script>title()</script>", markup)
                self.assertNotIn("<script>alert(42)</script>", markup)
                self.assertIn("&lt;script&gt;", markup)

    def test_editor_shell_and_old_routes_and_private_files_are_not_public(self):
        editor = self.client.get("/shygarma")
        self.assertEqual(editor.status_code, 200)
        self.assertIn("password", editor.get_data(as_text=True).lower())
        paths = (
            "/music", "/songs.json", "/index.html", "/app.py", "/posts.py",
            "/.env", "/.cache", "/.git/config", "/.local-backups/", "/static/.env",
            "/static/../.env", "/static/../app.py", "/static/../.local-backups/",
        )
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)
        for path in ("/", "/posts"):
            self.assertNotIn("/shygarma", self.client.get(path).get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
