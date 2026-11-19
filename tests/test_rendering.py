import unittest

from markupsafe import Markup

from rendering import image_ids, render_body, validate_body


def document(*nodes):
    return {"type": "doc", "content": list(nodes)}


def paragraph(text, marks=None):
    node = {"type": "text", "text": text}
    if marks is not None:
        node["marks"] = marks
    return {"type": "paragraph", "content": [node]}


def link(href):
    return {"type": "link", "attrs": {"href": href}}


class RenderingTests(unittest.TestCase):
    def test_supported_blocks_and_marks(self):
        body = document(
            {"type": "heading", "attrs": {"level": 2}, "content": [{"type": "text", "text": "Section"}]},
            {"type": "heading", "attrs": {"level": 3}, "content": [{"type": "text", "text": "Detail"}]},
            {"type": "paragraph", "content": [
                {"type": "text", "text": "First", "marks": [{"type": "bold"}, {"type": "italic"}]},
                {"type": "hardBreak"},
                {"type": "text", "text": "Second"},
            ]},
            {"type": "bulletList", "content": [
                {"type": "listItem", "content": [paragraph("Bullet")]},
            ]},
            {"type": "orderedList", "content": [
                {"type": "listItem", "content": [paragraph("Numbered")]},
            ]},
        )
        result = render_body(body)
        self.assertIsInstance(result, Markup)
        self.assertEqual(str(result), "<h2>Section</h2><h3>Detail</h3>"
                         "<p><em><strong>First</strong></em><br>Second</p>"
                         "<ul><li><p>Bullet</p></li></ul>"
                         "<ol><li><p>Numbered</p></li></ol>")

    def test_text_and_link_attributes_are_escaped(self):
        result = str(render_body(document(paragraph(
            '<script>alert("x")</script>&',
            [link('https://example.test/?q="&x=<value>')],
        ))))
        self.assertIn('href="https://example.test/?q=&#34;&amp;x=&lt;value&gt;"', result)
        self.assertIn('&lt;script&gt;alert(&#34;x&#34;)&lt;/script&gt;&amp;', result)
        self.assertNotIn("<script>", result)

    def test_only_safe_link_targets_are_allowed(self):
        safe = ["https://example.test/path", "HTTP://example.test/", "/posts/example", "/", "/?q=a%20b"]
        for href in safe:
            with self.subTest(href=href):
                self.assertIn("<a href=", str(render_body(document(paragraph("Link", [link(href)])))))
        unsafe = [
            "javascript:alert(1)", "JaVaScRiPt:alert(1)", "data:text/html,<script>",
            "mailto:person@example.test", "relative/path", "//evil.test", "///evil.test",
            "/\\evil.test", "https://example.test/\\evil", "https://example.test/\nnext",
            "\tjavascript:alert(1)", "https://example.test/\x00", "/%5cevil.test",
            "/%2fevil.test", "https://example.test/%0aevil", "https://example.test/%7f",
            "https://example.test/\u200b", " https://example.test", "http://", "https://[invalid",
        ]
        for href in unsafe:
            with self.subTest(href=repr(href)):
                self.assertEqual(str(render_body(document(paragraph("Link", [link(href)])))), "<p>Link</p>")

    def test_unknown_nodes_are_ignored_and_unknown_marks_keep_text(self):
        body = document(
            {"type": "html", "content": [paragraph("Hidden")], "text": "<script>bad()</script>"},
            {"type": "image", "attrs": {"src": "javascript:bad()"}},
            paragraph("Visible", [{"type": "madeUp", "attrs": {"onclick": "bad()"}}]),
        )
        self.assertEqual(str(render_body(body)), "<p>Visible</p>")

    def test_malformed_documents_and_nodes_do_not_render_markup(self):
        for body in [None, [], "<script>", {}, {"type": "paragraph"}, {"type": "doc", "content": {}}]:
            with self.subTest(body=body):
                self.assertEqual(str(render_body(body)), "")
        body = document(None, "text", {"type": "paragraph", "content": "<script>"},
                        {"type": "heading", "attrs": {"level": '2 onclick="bad()"'}, "content": []})
        self.assertEqual(str(render_body(body)), "")

    def test_blocks_stay_out_of_paragraphs_and_links_do_not_nest(self):
        body = document({"type": "paragraph", "content": [
            {"type": "heading", "attrs": {"level": 2}, "content": [{"type": "text", "text": "No"}]},
            {"type": "text", "text": "Yes", "marks": [link("/one"), link("/two")]},
        ]}, {"type": "listItem", "content": [paragraph("Outside a list")]})
        self.assertEqual(str(render_body(body)), '<p><a href="/one">Yes</a></p>')

    def test_nested_lists_have_a_bounded_recursion_depth(self):
        nested = paragraph("Leaf")
        for _ in range(100):
            nested = {"type": "bulletList", "content": [{"type": "listItem", "content": [nested]}]}
        result = str(render_body(document(nested)))
        self.assertEqual(result.count("<ul>"), result.count("</ul>"))
        self.assertLessEqual(result.count("<ul>"), 33)

    def test_editor_marks_quotes_code_and_spoilers_escape_text(self):
        result = str(render_body(document(
            paragraph("underline", [{"type": "underline"}]),
            paragraph("removed", [{"type": "strike"}]),
            paragraph("<inline>", [{"type": "code"}]),
            paragraph("surprise", [{"type": "spoiler"}, link("/not-nested")]),
            {"type": "blockquote", "content": [paragraph("A quote")]},
            {"type": "codeBlock", "attrs": {"language": 'evil onclick="bad()"'}, "content": [{"type": "text", "text": '<script>\n  code & more'}]},
        )))
        self.assertIn("<u>underline</u>", result)
        self.assertIn("<s>removed</s>", result)
        self.assertIn("<code>&lt;inline&gt;</code>", result)
        self.assertIn('<button type="button" class="spoiler" aria-expanded="false" aria-label="Reveal spoiler">surprise</button>', result)
        self.assertNotIn("/not-nested", result)
        self.assertIn("<blockquote><p>A quote</p></blockquote>", result)
        self.assertIn("<pre><code>&lt;script&gt;\n  code &amp; more</code></pre>", result)
        self.assertNotIn("onclick", result)

    def test_images_require_a_local_resolver_and_escape_alt_and_caption(self):
        upload_id = "a" * 32
        body = document({"type": "image", "attrs": {"uploadId": upload_id, "alt": 'Alt "<tag>"', "caption": "Caption <script>"}})
        self.assertEqual(str(render_body(body)), "")
        self.assertEqual(image_ids(body), {upload_id})
        rendered = str(render_body(body, lambda value: "/media/" + value))
        self.assertIn('src="/media/' + upload_id + '"', rendered)
        self.assertIn('alt="Alt &#34;&lt;tag&gt;&#34;"', rendered)
        self.assertIn("<figcaption>Caption &lt;script&gt;</figcaption>", rendered)
        for unsafe in ("https://evil.test/a.png", "//evil.test/a.png", "/%2fevil.test", "/\\evil.test", "javascript:alert(1)"):
            with self.subTest(unsafe=unsafe):
                self.assertEqual(str(render_body(body, lambda value: unsafe)), "")

    def test_validation_normalizes_and_bounds_editor_documents(self):
        body = document({"type": "paragraph", "attrs": {"onclick": "bad()"}, "content": [
            {"type": "text", "text": "A link", "marks": [{"type": "link", "attrs": {"href": "/posts/example", "target": "_blank"}}]},
        ]})
        normalized = validate_body(body)
        self.assertNotIn("attrs", normalized["content"][0])
        self.assertEqual(normalized["content"][0]["content"][0]["marks"][0]["attrs"], {"href": "/posts/example"})
        bad = [document({"type": "html", "text": "<script>"}),
               document(paragraph("Bad link", [link("javascript:alert(1)")])),
               document({"type": "image", "attrs": {"src": "https://example.test/image.png"}}),
               document({"type": "paragraph", "content": [{"type": "blockquote", "content": []}]}),
               document(paragraph("x" * 1_048_577))]
        nested = paragraph("Deep")
        for _ in range(35):
            nested = {"type": "blockquote", "content": [nested]}
        bad.append(document(nested))
        for item in bad:
            with self.subTest(item_type=item["content"][0]["type"]):
                with self.assertRaises(ValueError):
                    validate_body(item)


if __name__ == "__main__":
    unittest.main()
