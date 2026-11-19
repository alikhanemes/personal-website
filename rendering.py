"""The single safe renderer and validation boundary for article documents."""

import json
import re
from urllib.parse import unquote, urlsplit
import unicodedata

from markupsafe import Markup, escape


_MAX_DEPTH = 32
MAX_BODY_BYTES = 1_048_576
_BLOCKS = {"paragraph", "heading", "bulletList", "orderedList", "blockquote", "codeBlock", "image"}
_MARKS = {"bold", "italic", "underline", "strike", "link", "code", "spoiler"}


def _has_controls(value: str) -> bool:
    return any(unicodedata.category(char) in {"Cc", "Cf"} for char in value)


def _safe_href(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    # Browsers normalize some whitespace and backslashes before navigating.
    if "\\" in value or _has_controls(value) or any(char.isspace() for char in value):
        return None
    decoded = unquote(value)
    if "\\" in decoded or _has_controls(decoded):
        return None
    if value.startswith("/"):
        return value if not decoded.startswith("//") else None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme.lower() in {"http", "https"} and parsed.netloc:
        return value
    return None


def validate_body(body: object) -> dict:
    """Return a normalized, bounded Tiptap document or raise ValueError.

    Stored HTML, arbitrary attributes, external image sources and unsupported
    editor extensions never enter the document model. The renderer remains
    defensive for preexisting rows and callers that did not use this boundary.
    """
    if not isinstance(body, dict) or body.get("type") != "doc" or not isinstance(body.get("content"), list):
        raise ValueError("A body must be a Tiptap document with a content array.")
    try:
        if len(json.dumps(body, ensure_ascii=False).encode("utf-8")) > MAX_BODY_BYTES:
            raise ValueError("Article body exceeds 1 MiB.")
    except (TypeError, RecursionError, UnicodeError) as error:
        raise ValueError("Article body must be valid JSON.") from error
    count = 0

    def clean(node, context, depth):
        nonlocal count
        count += 1
        if count > 10_000 or depth > _MAX_DEPTH or not isinstance(node, dict):
            raise ValueError("Article document is too complex or malformed.")
        kind = node.get("type")
        allowed = {"text", "hardBreak"} if context == "inline" else {"listItem"} if context == "list" else _BLOCKS
        if kind not in allowed:
            raise ValueError("Unsupported article element.")
        result = {"type": kind}
        attrs = node.get("attrs") or {}
        if not isinstance(attrs, dict):
            raise ValueError("Element attributes must be an object.")
        if kind == "text":
            if not isinstance(node.get("text"), str):
                raise ValueError("Text elements need text.")
            result["text"] = node["text"]
            marks = node.get("marks", [])
            if not isinstance(marks, list) or len(marks) > len(_MARKS):
                raise ValueError("Invalid text formatting.")
            cleaned_marks, seen = [], set()
            for mark in marks:
                if not isinstance(mark, dict) or mark.get("type") not in _MARKS or mark["type"] in seen:
                    raise ValueError("Unsupported or repeated text formatting.")
                seen.add(mark["type"])
                cleaned = {"type": mark["type"]}
                if mark["type"] == "link":
                    mark_attrs = mark.get("attrs") or {}
                    href = _safe_href(mark_attrs.get("href")) if isinstance(mark_attrs, dict) else None
                    if href is None:
                        raise ValueError("Links must use HTTP, HTTPS, or a local absolute path.")
                    cleaned["attrs"] = {"href": href}
                cleaned_marks.append(cleaned)
            if cleaned_marks:
                result["marks"] = cleaned_marks
            return result
        if kind == "hardBreak":
            return result
        if kind == "image":
            upload_id = attrs.get("uploadId")
            if not isinstance(upload_id, str) or not re.fullmatch(r"[0-9a-f]{32}", upload_id):
                raise ValueError("Images must reference a local upload.")
            image_attrs = {"uploadId": upload_id}
            for name, maximum in (("alt", 1000), ("caption", 4000)):
                value = attrs.get(name, "")
                if not isinstance(value, str) or len(value) > maximum:
                    raise ValueError(f"Image {name} is too long or invalid.")
                image_attrs[name] = value
            result["attrs"] = image_attrs
            return result
        if kind == "heading":
            if type(attrs.get("level")) is not int or attrs["level"] not in {2, 3}:
                raise ValueError("Article headings must be level 2 or 3.")
            result["attrs"] = {"level": attrs["level"]}
        if kind == "codeBlock":
            language = attrs.get("language")
            if language is not None and (not isinstance(language, str) or len(language) > 50):
                raise ValueError("Invalid code language.")
            if language:
                result["attrs"] = {"language": language}
        content = node.get("content", [])
        if not isinstance(content, list):
            raise ValueError("Element content must be an array.")
        next_context = "inline" if kind in {"paragraph", "heading", "codeBlock"} else "list" if kind in {"bulletList", "orderedList"} else "block"
        result["content"] = [clean(child, next_context, depth + 1) for child in content]
        if kind == "codeBlock" and any(child["type"] != "text" or child.get("marks") for child in result["content"]):
            raise ValueError("Code blocks contain plain text only.")
        return result

    return {"type": "doc", "content": [clean(node, "block", 0) for node in body["content"]]}


def image_ids(body: object) -> set[str]:
    """Collect upload references from a validated document, without rendering."""
    found = set()

    def visit(node, depth):
        if not isinstance(node, dict) or depth > _MAX_DEPTH:
            return
        if node.get("type") == "image":
            attrs = node.get("attrs")
            upload_id = attrs.get("uploadId") if isinstance(attrs, dict) else None
            if isinstance(upload_id, str) and re.fullmatch(r"[0-9a-f]{32}", upload_id):
                found.add(upload_id)
        children = node.get("content")
        if isinstance(children, list):
            for child in children:
                visit(child, depth + 1)

    visit(body, -1)
    return found


def _inline(nodes: object, depth: int) -> str:
    if not isinstance(nodes, list) or depth > _MAX_DEPTH:
        return ""
    rendered = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        if node.get("type") == "hardBreak":
            rendered.append("<br>")
        elif node.get("type") == "text" and isinstance(node.get("text"), str):
            text = str(escape(node["text"]))
            linked = False
            marks = node.get("marks", [])
            has_spoiler = isinstance(marks, list) and any(isinstance(mark, dict) and mark.get("type") == "spoiler" for mark in marks)
            for mark in marks if isinstance(marks, list) else []:
                if not isinstance(mark, dict):
                    continue
                kind = mark.get("type")
                if kind == "bold":
                    text = f"<strong>{text}</strong>"
                elif kind == "italic":
                    text = f"<em>{text}</em>"
                elif kind == "underline":
                    text = f"<u>{text}</u>"
                elif kind == "strike":
                    text = f"<s>{text}</s>"
                elif kind == "code":
                    text = f"<code>{text}</code>"
                elif kind == "link" and not linked and not has_spoiler:
                    attrs = mark.get("attrs")
                    href = _safe_href(attrs.get("href")) if isinstance(attrs, dict) else None
                    if href is not None:
                        text = f'<a href="{escape(href)}">{text}</a>'
                        linked = True
            if has_spoiler:
                text = f'<button type="button" class="spoiler" aria-expanded="false" aria-label="Reveal spoiler">{text}</button>'
            rendered.append(text)
    return "".join(rendered)


def _blocks(nodes: object, depth: int = 0, image_resolver=None) -> str:
    if not isinstance(nodes, list) or depth > _MAX_DEPTH:
        return ""
    rendered = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        kind = node.get("type")
        content = node.get("content", [])
        if not isinstance(content, list):
            continue
        if kind == "paragraph":
            rendered.append(f"<p>{_inline(content, depth + 1)}</p>")
        elif kind == "heading":
            attrs = node.get("attrs")
            level = attrs.get("level") if isinstance(attrs, dict) else None
            if type(level) is int and level in {2, 3}:
                rendered.append(f"<h{level}>{_inline(content, depth + 1)}</h{level}>")
        elif kind in {"bulletList", "orderedList"}:
            tag = "ul" if kind == "bulletList" else "ol"
            items = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "listItem":
                    children = item.get("content", [])
                    if isinstance(children, list):
                        items.append(f"<li>{_blocks(children, depth + 1, image_resolver)}</li>")
            rendered.append(f"<{tag}>{''.join(items)}</{tag}>")
        elif kind == "blockquote":
            rendered.append(f"<blockquote>{_blocks(content, depth + 1, image_resolver)}</blockquote>")
        elif kind == "codeBlock":
            code = "".join(str(escape(child.get("text", ""))) for child in content if isinstance(child, dict) and child.get("type") == "text" and isinstance(child.get("text"), str))
            rendered.append(f"<pre><code>{code}</code></pre>")
        elif kind == "image" and image_resolver is not None:
            attrs = node.get("attrs")
            if not isinstance(attrs, dict):
                continue
            upload_id = attrs.get("uploadId")
            if not isinstance(upload_id, str) or not re.fullmatch(r"[0-9a-f]{32}", upload_id):
                continue
            url = image_resolver(upload_id)
            if not isinstance(url, str) or not url.startswith("/") or _safe_href(url) is None:
                continue
            alt = attrs.get("alt", "")
            caption = attrs.get("caption", "")
            alt = alt if isinstance(alt, str) else ""
            caption = caption if isinstance(caption, str) else ""
            figure = f'<figure class="article-figure"><img src="{escape(url)}" alt="{escape(alt)}" loading="lazy">'
            if caption:
                figure += f"<figcaption>{escape(caption)}</figcaption>"
            rendered.append(figure + "</figure>")
    return "".join(rendered)


def render_body(body: dict, image_resolver=None) -> Markup:
    """Return safe HTML for a Tiptap/ProseMirror document; ignore unknown nodes."""
    if not isinstance(body, dict) or body.get("type") != "doc":
        return Markup("")
    return Markup(_blocks(body.get("content"), image_resolver=image_resolver))
