"""SQLite post storage and repeatable local setup commands."""

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import click
from flask import current_app, g
from rendering import image_ids, validate_body


class EditConflict(Exception):
    """The client's edit version is stale; it must reload before saving."""


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(current_app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def close_db(error=None):
    connection = g.pop("db", None)
    if connection is not None:
        connection.close()


def init_db():
    get_db().executescript("""
        CREATE TABLE IF NOT EXISTS posts (
            id INTEGER PRIMARY KEY,
            slug TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            published_at TEXT,
            CHECK (published_at IS NULL OR julianday(published_at) IS NOT NULL)
        );
        CREATE INDEX IF NOT EXISTS posts_publication ON posts(published_at);
        CREATE TABLE IF NOT EXISTS post_edits (
            post_id INTEGER PRIMARY KEY REFERENCES posts(id) ON DELETE CASCADE,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        );
    """)
    db = get_db()
    columns = {row["name"] for row in db.execute("PRAGMA table_info(posts)")}
    if "archived_at" not in columns:
        db.execute("ALTER TABLE posts ADD COLUMN archived_at TEXT")
    db.execute("""
        CREATE TABLE IF NOT EXISTS deleted_posts (
            slug TEXT PRIMARY KEY,
            deleted_at TEXT NOT NULL
        )
    """)
    db.commit()
    # Schema additions are idempotent; existing published rows and URLs stay put.
    from media import init_media_schema
    init_media_schema()


def _title(value):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 300:
        raise ValueError("A post needs a title of 1–300 characters.")
    return value.strip()


def _now():
    return publication_time(datetime.now(timezone.utc))


def publication_time(value):
    """Store timezone-aware publication timestamps as canonical UTC ISO 8601."""
    if value is None:
        return None
    moment = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    if not isinstance(moment, datetime) or moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("Publication timestamps must include a timezone.")
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def create_post(slug, title, body, published_at=None, *, skip_existing=False):
    """Write boundary for seeds and a future editor; never changes a slug."""
    if not isinstance(slug, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
        raise ValueError("Use a stable lowercase, hyphen-separated slug.")
    title = _title(title)
    body = validate_body(body)
    conflict = " ON CONFLICT(slug) DO NOTHING" if skip_existing else ""
    db = get_db()
    if db.execute("SELECT 1 FROM deleted_posts WHERE slug = ?", (slug,)).fetchone():
        if skip_existing:
            return 0
        raise ValueError("This permalink was permanently deleted and cannot be reused.")
    cursor = db.execute(
        "INSERT INTO posts (slug, title, body, published_at) VALUES (?, ?, ?, ?)" + conflict,
        (slug, title, json.dumps(body, ensure_ascii=False), publication_time(published_at)),
    )
    db.commit()
    return cursor.rowcount


def _post(row):
    if row is None:
        return None
    post = dict(row)
    post["body"] = json.loads(post["body"])
    post["status"] = "archived" if post.get("archived_at") else "published" if post["published_at"] else "draft"
    post["date_label"] = None
    if post["published_at"]:
        moment = datetime.fromisoformat(post["published_at"].replace("Z", "+00:00"))
        post["date_label"] = f"{moment.day} {moment.strftime('%B %Y')}"
    return post


# Fixed-width UTC strings retain microseconds (SQLite julianday rounds them).
# IDs break exact timestamp ties consistently on Main and in the archive.
PUBLIC_ORDER = "ORDER BY published_at DESC, id DESC"


def latest_published():
    return _post(get_db().execute(
        f"SELECT * FROM posts WHERE published_at IS NOT NULL AND archived_at IS NULL {PUBLIC_ORDER} LIMIT 1"
    ).fetchone())


def older_published():
    return [_post(row) for row in get_db().execute(
        f"SELECT * FROM posts WHERE published_at IS NOT NULL AND archived_at IS NULL {PUBLIC_ORDER} LIMIT -1 OFFSET 1"
    ).fetchall()]


def published_by_slug(slug):
    return _post(get_db().execute(
        "SELECT * FROM posts WHERE slug = ? AND published_at IS NOT NULL AND archived_at IS NULL", (slug,)
    ).fetchone())


_EDIT_COLUMNS = "p.id, p.slug, e.title, e.body, p.published_at, p.archived_at, e.version, e.updated_at"


def editor_list_posts():
    """Private list includes autosaved working titles without exposing their body."""
    rows = get_db().execute("""
        SELECT p.id, p.slug, COALESCE(e.title, p.title) AS title,
               p.published_at, p.archived_at, e.version, e.updated_at,
               CASE WHEN p.archived_at IS NOT NULL THEN 'archived'
                    WHEN p.published_at IS NOT NULL THEN 'published' ELSE 'draft' END AS status
        FROM posts AS p LEFT JOIN post_edits AS e ON e.post_id = p.id
        ORDER BY COALESCE(e.updated_at, p.published_at) DESC, p.id DESC
    """).fetchall()
    return [dict(row) for row in rows]


def get_edit(post_id):
    """Open a working copy; never mutate the public post during autosave."""
    db = get_db()
    if db.execute("SELECT id FROM posts WHERE id = ?", (post_id,)).fetchone() is None:
        return None
    db.execute("""
        INSERT INTO post_edits (post_id, title, body, version, updated_at)
        SELECT id, title, body, 0, ? FROM posts WHERE id = ?
        ON CONFLICT(post_id) DO NOTHING
    """, (_now(), post_id))
    db.commit()
    return _post(db.execute(
        f"SELECT {_EDIT_COLUMNS} FROM posts AS p JOIN post_edits AS e ON e.post_id = p.id WHERE p.id = ?",
        (post_id,),
    ).fetchone())


def new_draft(title="Untitled draft"):
    db = get_db()
    title = _title(title)
    blank = json.dumps({"type": "doc", "content": []})
    cursor = db.execute(
        "INSERT INTO posts (slug, title, body, published_at) VALUES (?, ?, ?, NULL)",
        ("post-" + uuid4().hex, title, blank),
    )
    post_id = cursor.lastrowid
    db.execute("INSERT INTO post_edits (post_id, title, body, version, updated_at) VALUES (?, ?, ?, 0, ?)",
               (post_id, title, blank, _now()))
    db.commit()
    return get_edit(post_id)


def _version(value):
    if type(value) is not int or value < 0:
        raise ValueError("A nonnegative edit version is required.")
    return value


def save_edit(post_id, title, body, expected_version):
    title = _title(title)
    body = validate_body(body)
    expected_version = _version(expected_version)
    db = get_db()
    cursor = db.execute("""
        UPDATE post_edits SET title = ?, body = ?, version = version + 1, updated_at = ?
        WHERE post_id = ? AND version = ?
    """, (title, json.dumps(body, ensure_ascii=False), _now(), post_id, expected_version))
    if cursor.rowcount != 1:
        db.rollback()
        if db.execute("SELECT id FROM posts WHERE id = ?", (post_id,)).fetchone() is None:
            raise ValueError("Post not found.")
        raise EditConflict("This article changed in another request. Reload before saving.")
    db.commit()
    return get_edit(post_id)


def publish_edit(post_id, expected_version, notify_subscribers=False):
    """Commit the exact saved revision; retain existing publication time and slug."""
    from media import get_upload

    expected_version = _version(expected_version)
    db = get_db()
    try:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            f"SELECT {_EDIT_COLUMNS} FROM posts AS p JOIN post_edits AS e ON e.post_id = p.id WHERE p.id = ?",
            (post_id,),
        ).fetchone()
        if row is None:
            raise ValueError("Post not found.")
        if row["version"] != expected_version:
            raise EditConflict("This article changed before publication. Reload before publishing.")
        if row["archived_at"] is not None:
            raise ValueError("Restore this article before updating its publication.")
        body = validate_body(json.loads(row["body"]))
        for upload_id in image_ids(body):
            if get_upload(upload_id) is None:
                raise ValueError("An article image is missing. Reinsert it before publishing.")
        published_at = row["published_at"]
        if published_at is None:
            moment = datetime.now(timezone.utc)
            # Phase 1 seeds may have later timestamps than the laptop clock.
            # A new publication must still become Main, without touching them.
            latest = db.execute("SELECT published_at FROM posts WHERE published_at IS NOT NULL ORDER BY published_at DESC LIMIT 1").fetchone()
            if latest:
                latest_moment = datetime.fromisoformat(latest["published_at"].replace("Z", "+00:00"))
                moment = max(moment, latest_moment + timedelta(microseconds=1))
            published_at = publication_time(moment)
        db.execute("UPDATE posts SET title = ?, body = ?, published_at = ? WHERE id = ?",
                   (_title(row["title"]), json.dumps(body, ensure_ascii=False), published_at, post_id))
        if row["published_at"] is None and db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='notification_events'"
        ).fetchone():
            from newsletter import queue_first_publication
            queue_first_publication(post_id, notify_subscribers, db=db)
        db.execute("UPDATE post_edits SET version = version + 1, updated_at = ? WHERE post_id = ?", (_now(), post_id))
        db.commit()
    except Exception:
        db.rollback()
        raise
    return get_edit(post_id)


def _lifecycle_row(db, post_id, expected_version):
    row = db.execute("""
        SELECT p.*, COALESCE(e.version, 0) AS version
        FROM posts AS p LEFT JOIN post_edits AS e ON e.post_id = p.id
        WHERE p.id = ?
    """, (post_id,)).fetchone()
    if row is None:
        raise ValueError("Post not found.")
    if row["version"] != _version(expected_version):
        raise EditConflict("This article changed in another request. Reload before continuing.")
    return row


def _advance_edit(db, post_id):
    # The lifecycle action also invalidates an autosave already in flight.
    db.execute("""
        INSERT INTO post_edits (post_id, title, body, version, updated_at)
        SELECT id, title, body, 1, ? FROM posts WHERE id = ?
        ON CONFLICT(post_id) DO UPDATE SET version = version + 1, updated_at = excluded.updated_at
    """, (_now(), post_id))


def _cancel_notifications(db, post_id, *, remove=False):
    """Newsletter integration remains optional for storage-only tools/tests."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='notification_events'").fetchone():
        return
    from newsletter import cancel_post_notifications, delete_post_notifications
    if remove:
        delete_post_notifications(post_id, db=db)
    else:
        cancel_post_notifications(post_id, db=db)


def archive_post(post_id, expected_version):
    """Withdraw a publication without altering its text, URL, date or edits."""
    db = get_db()
    try:
        db.execute("BEGIN IMMEDIATE")
        row = _lifecycle_row(db, post_id, expected_version)
        if row["published_at"] is None or row["archived_at"] is not None:
            raise ValueError("Only a currently published article can be archived.")
        db.execute("UPDATE posts SET archived_at = ? WHERE id = ?", (_now(), post_id))
        _advance_edit(db, post_id)
        _cancel_notifications(db, post_id)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return get_edit(post_id)


def restore_post(post_id, expected_version):
    """Restore the original public revision; never publish the working copy."""
    from media import get_upload
    db = get_db()
    try:
        db.execute("BEGIN IMMEDIATE")
        row = _lifecycle_row(db, post_id, expected_version)
        if row["published_at"] is None or row["archived_at"] is None:
            raise ValueError("Only an archived publication can be restored.")
        for upload_id in image_ids(json.loads(row["body"])):
            if get_upload(upload_id) is None:
                raise ValueError("An article image is missing. Repair the image before restoring.")
        db.execute("UPDATE posts SET archived_at = NULL WHERE id = ?", (post_id,))
        _advance_edit(db, post_id)
        # Restoration must not revive an old pending new-post broadcast.
        _cancel_notifications(db, post_id)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return get_edit(post_id)


def permanently_delete_post(post_id, confirmation, expected_version):
    """Delete storage atomically, then retryably remove unshared article media."""
    from media import cleanup_media, enqueue_orphan_media
    if confirmation != "DELETE":
        raise ValueError("Type DELETE exactly to permanently delete this article.")
    db = get_db()
    try:
        db.execute("BEGIN IMMEDIATE")
        row = _lifecycle_row(db, post_id, expected_version)
        affected = image_ids(json.loads(row["body"]))
        edit = db.execute("SELECT body FROM post_edits WHERE post_id = ?", (post_id,)).fetchone()
        if edit:
            affected.update(image_ids(json.loads(edit["body"])))
        _cancel_notifications(db, post_id, remove=True)
        # post_edits has ON DELETE CASCADE. There is no revision table in the
        # phase 2 schema; a future revision table must use the same constraint.
        db.execute("DELETE FROM posts WHERE id = ?", (post_id,))
        db.execute("INSERT OR REPLACE INTO deleted_posts (slug, deleted_at) VALUES (?, ?)",
                   (row["slug"], _now()))
        enqueue_orphan_media(affected)
        db.commit()
    except Exception:
        db.rollback()
        raise
    # The database tombstone is already durable. A sidecar failure cannot undo
    # deletion; report it separately so backup operators know to retry it.
    try:
        from operations import persist_deletion_ledger
        ledger = persist_deletion_ledger()
    except (OSError, ValueError) as error:
        ledger = {"saved": False, "error": str(error)}
    return {"deleted": True, "slug": row["slug"], "cleanup": cleanup_media(), "deletion_ledger": ledger}


def init_app(app):
    app.teardown_appcontext(close_db)

    @app.cli.command("init-db")
    def init_db_command():
        """Create the schema without deleting any posts."""
        init_db()
        click.echo(f"Database ready: {current_app.config['DATABASE']}")

    @app.cli.command("seed")
    def seed_command():
        """Insert two sample articles and one draft, leaving existing slugs alone."""
        from sample_posts import SAMPLE_POSTS

        added = sum(create_post(**post, skip_existing=True) for post in SAMPLE_POSTS)
        click.echo(f"Added {added} sample posts. Existing posts were left unchanged.")

    @app.cli.command("cleanup-media")
    def cleanup_media_command():
        """Retry file removals interrupted during permanent post deletion."""
        from media import cleanup_media
        result = cleanup_media()
        click.echo(json.dumps(result))
        if result["pending"]:
            raise click.ClickException("Some media files remain queued; resolve the reported errors and retry.")
