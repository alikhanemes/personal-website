# Article body format

Posts store a JSON document compatible with Tiptap/ProseMirror's node shape. The
title and publication timestamp are separate post fields; do not repeat the title
as a body heading. An empty body is `{"type": "doc", "content": []}`.

```json
{
  "type": "doc",
  "content": [
    {
      "type": "paragraph",
      "content": [
        {"type": "text", "text": "An opening sentence. "},
        {"type": "text", "text": "Emphasis", "marks": [{"type": "bold"}]},
        {"type": "text", "text": " and a link", "marks": [
          {"type": "link", "attrs": {"href": "https://example.com/"}}
        ]}
      ]
    },
    {
      "type": "heading",
      "attrs": {"level": 2},
      "content": [{"type": "text", "text": "A section"}]
    }
  ]
}
```

Phase 2 supports these nodes:

| Node | Shape and output |
| --- | --- |
| `doc` | Root with a `content` array of blocks |
| `paragraph` | Inline `content`, rendered as `<p>` |
| `heading` | Inline `content`, with integer `attrs.level` of 2 or 3 |
| `text` | A string `text` and optional `marks` array |
| `hardBreak` | An inline line break |
| `bulletList` / `orderedList` | `content` containing `listItem` nodes |
| `listItem` | `content` containing paragraphs, headings, or nested lists |
| `blockquote` | Block `content`, rendered as `<blockquote>` |
| `codeBlock` | Unmarked text `content`, rendered as escaped `<pre><code>`; optional `attrs.language` is stored but does not inject HTML classes |
| `image` | Block with `attrs.uploadId`, `attrs.alt`, and `attrs.caption`; rendered as a figure with an image and optional caption |

Text marks are `bold`, `italic`, `underline`, `strike`, `code`, `spoiler`, and
`link`. Spoilers render as buttons that reveal their text when activated; they
are presentation controls, not a way to keep published text secret. Links on the
same text as a spoiler are suppressed to avoid nested interactive controls.
Link marks use `attrs.href`; other
attributes, including custom HTML attributes and targets, are ignored. Links may
use HTTP, HTTPS, or a path beginning with a single `/`. Protocol-relative links,
backslashes, control characters (including their encoded forms), and other URL
schemes are rejected. Rejected link marks retain their visible text. Ordered lists
currently begin at 1; custom list attributes are ignored.

An image refers to a server-generated upload ID, never a user-supplied URL:

```json
{
  "type": "image",
  "attrs": {
    "uploadId": "1ba86ec1ea30497cb11ad3e739fbfb67",
    "alt": "A description of the image",
    "caption": "An optional caption"
  }
}
```

Uploads live beneath the configured data directory in `uploads/`, outside
`static/`. Only a reference in the current published `posts.body` makes an image
public. A newly uploaded image, a draft image, or an image only in a published
article's working revision requires private route authorization. Removing the
last published reference on an explicit Update removes public access again.
Supported upload formats are PNG, JPEG, and single-frame WebP, limited to 8 MiB
and 24 megapixels. The server checks and decodes actual bytes, applies EXIF
orientation, and reencodes without metadata. SVG, GIF, and animation are rejected.

`rendering.py` is the only body-to-HTML renderer. It escapes text and link
attributes before returning safe Markup for Jinja. Unsupported nodes and their
contents are ignored; unsupported marks retain their text. Malformed documents
return empty output. Blocks are only rendered in block contexts, list items only
inside lists, and repeated link marks cannot create nested anchors. A depth limit
protects rendering from excessively nested bodies. A caller must supply an image
resolver to render figures; only safe root-relative resolver URLs are allowed.

The editor save boundary validates and normalizes this schema before storage.
Documents are limited to 1 MiB of JSON, 10,000 nodes, and 32 levels of nesting.
Titles contain 1–300 characters, image alt text at most 1,000 characters, and
captions at most 4,000. Unsupported nodes/marks and unsafe link URLs are rejected
on save. Harmless extra editor attributes are stripped. No raw HTML is supported.

`posts` retains each stable slug and the last explicitly published title/body.
A null `published_at` means draft. `post_edits` contains a private working copy,
its integer version, and last save time. Autosaves compare the client's expected
version in one SQL update and increment the stored version; stale requests fail
instead of replacing newer edits. Publish and Update commit the exact requested
revision and advance its version. The first Publish assigns a publication time;
later Updates preserve both the original timestamp and permanent slug. Opening
an existing phase 1 post creates its working copy without changing public data.

Phase 3 adds nullable `archived_at`. An archived post retains `published_at` and
its last published body while all public queries exclude it. Restore clears
`archived_at` without replacing the published body with its working copy. All
lifecycle mutations also compare/advance the working-copy version. Permanent
deletion removes the article and its working copy, records only the private
slug/deletion timestamp for recovery, and queues unshared images for retryable
cleanup. The structured body format itself is unchanged.
