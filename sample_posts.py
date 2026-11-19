"""Clearly labeled sample content; fixed slugs make repeated seeding harmless."""


def paragraph(text):
    return {"type": "paragraph", "content": [{"type": "text", "text": text}]}


def heading(text):
    return {"type": "heading", "attrs": {"level": 2}, "content": [{"type": "text", "text": text}]}


SAMPLE_POSTS = [
    {
        "slug": "sample-a-place-for-longer-thoughts",
        "title": "Sample article: A place for longer thoughts",
        "published_at": "2026-11-18T09:00:00Z",
        "body": {"type": "doc", "content": [
            paragraph("This is a sample article, written to show how a finished post will look. The words here are placeholders; the space is ready for something more personal."),
            paragraph("A good place to read can be a quiet one. A headline, a date, and a page with enough room for an idea to unfold. Nothing has to compete with the writing."),
            paragraph("Short updates have their uses, but some thoughts need a little more time. An observation becomes a question. A question leads to a story. Writing it down makes it possible to return to it, revise it, or see it differently a year later."),
            heading("Room to follow an idea"),
            paragraph("An article does not need to settle every question it raises. Sometimes its purpose is simply to pay closer attention: to a book, a conversation, a place, or a small detail that would otherwise disappear into the week."),
            paragraph("That is the kind of space this sample is meant to demonstrate. Paragraphs can be long enough to carry a thought, with pauses between them. The page stays simple as the writing gets more involved."),
            heading("A few things to bring along"),
            {"type": "bulletList", "content": [
                {"type": "listItem", "content": [paragraph("An observation worth returning to.")]},
                {"type": "listItem", "content": [paragraph("A question that has not found an answer yet.")]},
                {"type": "listItem", "content": [paragraph("The patience to follow both a little further.")]},
            ]},
            paragraph("Each published article has a permanent address. The newest one also appears in full on Main; earlier pieces have a home in Posts. The writing can grow while those simple paths stay the same."),
            paragraph("This is where the next article will begin."),
        ]},
    },
    {
        "slug": "sample-keeping-a-notebook",
        "title": "Sample article: Keeping a notebook",
        "published_at": "2026-11-10T09:00:00Z",
        "body": {"type": "doc", "content": [
            paragraph("This is the older sample article. It gives the archive a real entry and shows how every piece keeps its own permanent page."),
            paragraph("Imagine a notebook filled with small observations. A sentence from a conversation. A street seen for the first time. A question written in the margin of a book. Most entries do not begin as articles; they begin as reminders to look again."),
            heading("Small notes, kept over time"),
            paragraph("The useful part is the habit of noticing. A few lines recorded today might meet an idea from next month. Kept together, ordinary details can become something worth sharing."),
            paragraph("This sample ends here. It can be replaced with real writing when the private editor arrives."),
        ]},
    },
    {
        "slug": "sample-unpublished-draft",
        "title": "Sample draft: Not ready to publish",
        "published_at": None,
        "body": {"type": "doc", "content": [paragraph("This sample draft must never appear on a public page.")]},
    },
]
