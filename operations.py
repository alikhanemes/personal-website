"""Private SQLite/media backups and held-for-review restoration commands.

Backups contain subscriber addresses and private writing. They deliberately omit
admin.json, provider credentials and caches. A restore always closes the public
gate until the owner explicitly reviews and approves the restored data.
"""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
from uuid import uuid4

import click
from flask import current_app

import posts
from rendering import image_ids, validate_body


MARKER = "restore-review-required.json"
LEDGER = "deletion-ledger.json"
_MEDIA = re.compile(r"[0-9a-f]{32}\.(?:png|jpg|webp)")
_OPTIONAL_SECRET_FILES = {"newsletter-secret.key", "newsletter-secret"}


def _now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _data_dir():
    return Path(current_app.config["DATA_DIR"]).resolve()


def _sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path, value):
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _ledger(value):
    if not isinstance(value, dict) or value.get("version") != 1 or not isinstance(value.get("deleted_posts"), list):
        raise ValueError("Invalid deletion ledger.")
    result = {}
    for entry in value["deleted_posts"]:
        if (not isinstance(entry, dict) or not isinstance(entry.get("slug"), str) or
                not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", entry["slug"]) or
                not isinstance(entry.get("deleted_at"), str)):
            raise ValueError("Invalid deletion ledger entry.")
        posts.publication_time(entry["deleted_at"])
        result[entry["slug"]] = max(result.get(entry["slug"], ""), entry["deleted_at"])
    return result


def _read_ledger(path):
    try:
        return _ledger(json.loads(Path(path).read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise ValueError("Cannot verify the deletion ledger: " + str(error)) from error


def _db_deletions(db):
    exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='deleted_posts'").fetchone()
    return {row[0]: row[1] for row in db.execute("SELECT slug, deleted_at FROM deleted_posts")} if exists else {}


def _merge_deletions(*ledgers):
    merged = {}
    for ledger in ledgers:
        for slug, deleted_at in ledger.items():
            merged[slug] = max(merged.get(slug, ""), deleted_at)
    return merged


def _write_ledger(deletions):
    _atomic_json(_data_dir() / LEDGER, {"version": 1, "updated_at": _now(), "deleted_posts": [
        {"slug": slug, "deleted_at": deleted_at} for slug, deleted_at in sorted(deletions.items())
    ]})


def persist_deletion_ledger():
    """Keep a private metadata sidecar; failures must not undo a deleted post."""
    path = _data_dir() / LEDGER
    previous = _read_ledger(path) if path.exists() else {}
    deletions = _merge_deletions(previous, _db_deletions(posts.get_db()))
    _write_ledger(deletions)
    return {"saved": True, "entries": len(deletions)}


def _safe_backup_location(value, *, creating=False):
    directory = Path(value).expanduser().resolve()
    data = _data_dir()
    static = Path(current_app.static_folder).resolve()
    if directory == data or data in directory.parents or directory == static or static in directory.parents:
        raise ValueError("Backups must stay outside BLOG_DATA_DIR and public static files.")
    if creating and directory.exists():
        raise ValueError("Choose a new backup directory; existing paths are never overwritten.")
    return directory


def _validate_database(db, uploads_dir, *, manifest_files=None):
    db.row_factory = sqlite3.Row
    result = db.execute("PRAGMA integrity_check").fetchone()[0]
    if result != "ok":
        raise ValueError("SQLite integrity check failed: " + str(result))
    if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise ValueError("SQLite foreign-key check failed.")
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"posts", "post_edits", "uploads"}.issubset(tables):
        raise ValueError("Backup is not a compatible blog database.")
    referenced = set()
    for table in ("posts", "post_edits"):
        for row in db.execute("SELECT body FROM " + table):
            referenced.update(image_ids(validate_body(json.loads(row["body"]))))
    uploads = {row["id"]: dict(row) for row in db.execute("SELECT * FROM uploads")}
    pending = {row[0] for row in db.execute("SELECT upload_id FROM media_cleanup")} if "media_cleanup" in tables else set()
    for upload_id, upload in uploads.items():
        filename = upload["filename"]
        if not _MEDIA.fullmatch(filename) or not filename.startswith(upload_id + "."):
            raise ValueError("An upload has an unsafe stored filename.")
        path = uploads_dir / filename
        if upload_id not in pending and (path.is_symlink() or not path.is_file()):
            raise ValueError("An uploaded image is missing: " + upload_id)
        if manifest_files is not None and upload_id not in pending and "uploads/" + filename not in manifest_files:
            raise ValueError("An uploaded image is absent from the backup manifest.")
    for upload_id in referenced:
        if upload_id not in uploads or upload_id in pending:
            raise ValueError("An article references a missing or pending-deletion image: " + upload_id)
    columns = {row["name"] for row in db.execute("PRAGMA table_info(posts)")}
    public_where = "published_at IS NOT NULL" + (" AND archived_at IS NULL" if "archived_at" in columns else "")
    count = db.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
    public = db.execute("SELECT COUNT(*) FROM posts WHERE " + public_where).fetchone()[0]
    return {"posts": count, "public_posts": public, "images": len(uploads), "pending_cleanup": len(pending)}


def backup_data(output):
    """Snapshot SQLite and the exact indexed media under one writer lock."""
    destination = _safe_backup_location(output, creating=True)
    destination.mkdir(parents=True)
    db = posts.get_db()
    try:
        db.execute("BEGIN IMMEDIATE")
        source = sqlite3.connect(current_app.config["DATABASE"])
        target = sqlite3.connect(destination / "blog.sqlite3")
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        (destination / "uploads").mkdir()
        pending = {row[0] for row in db.execute("SELECT upload_id FROM media_cleanup")}
        for row in db.execute("SELECT id, filename FROM uploads"):
            filename = row["filename"]
            if not _MEDIA.fullmatch(filename) or not filename.startswith(row["id"] + "."):
                raise ValueError("Unsafe stored image filename.")
            path = _data_dir() / "uploads" / filename
            if path.is_symlink():
                raise ValueError("Uploaded image cannot be a symbolic link.")
            if path.exists():
                shutil.copyfile(path, destination / "uploads" / filename)
            elif row["id"] not in pending:
                raise ValueError("An uploaded image is missing; backup is incomplete.")
        for filename in _OPTIONAL_SECRET_FILES:
            path = _data_dir() / filename
            if path.is_file() and not path.is_symlink():
                shutil.copyfile(path, destination / filename)
        deletions = _db_deletions(db)
        sidecar = _data_dir() / LEDGER
        if sidecar.exists():
            deletions = _merge_deletions(deletions, _read_ledger(sidecar))
        db.commit()
    except Exception:
        db.rollback()
        raise
    snapshot = sqlite3.connect(destination / "blog.sqlite3")
    try:
        summary = _validate_database(snapshot, destination / "uploads")
    finally:
        snapshot.close()
    files = {path.relative_to(destination).as_posix(): _sha256(path)
             for path in destination.rglob("*") if path.is_file()}
    _atomic_json(destination / "manifest.json", {
        "version": 1, "created_at": _now(), "files": files, "summary": summary,
        "deleted_posts": [{"slug": slug, "deleted_at": when} for slug, when in sorted(deletions.items())],
    })
    return {"backup": str(destination), **summary, "verified": True}


def verify_backup(source):
    directory = _safe_backup_location(source)
    try:
        manifest_path = directory / "manifest.json"
        if manifest_path.is_symlink():
            raise ValueError("Backup manifest cannot be a symbolic link.")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or manifest.get("version") != 1 or not isinstance(manifest.get("files"), dict):
            raise ValueError("Unsupported backup manifest.")
        files = manifest["files"]
        if "blog.sqlite3" not in files:
            raise ValueError("Backup database is absent from the manifest.")
        for name, expected in files.items():
            relative = PurePosixPath(name)
            allowed = name == "blog.sqlite3" or name in _OPTIONAL_SECRET_FILES or (
                len(relative.parts) == 2 and relative.parts[0] == "uploads" and _MEDIA.fullmatch(relative.parts[1]))
            if not allowed or "\\" in name or not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
                raise ValueError("Backup contains an unsafe manifest entry.")
            path = directory.joinpath(*relative.parts)
            if path.is_symlink() or directory not in path.resolve().parents or _sha256(path) != expected:
                raise ValueError("Backup checksum failed: " + name)
        db = sqlite3.connect(directory / "blog.sqlite3")
        try:
            summary = _validate_database(db, directory / "uploads", manifest_files=files)
            deletions = _merge_deletions(_db_deletions(db), _ledger({
                "version": 1, "deleted_posts": manifest.get("deleted_posts", [])}))
        finally:
            db.close()
    except (OSError, ValueError, sqlite3.Error, TypeError, KeyError) as error:
        raise ValueError("Backup verification failed: " + str(error)) from error
    return directory, manifest, summary, deletions


def restore_data(source, *, deletions_path=None):
    """Restore privately and merge newer deletion history before any release."""
    directory, manifest, _, backup_deletions = verify_backup(source)
    current = _db_deletions(posts.get_db())
    sidecar = _data_dir() / LEDGER
    previous = _read_ledger(sidecar) if sidecar.exists() else {}
    external = _read_ledger(deletions_path) if deletions_path else {}
    deletions = _merge_deletions(current, previous, backup_deletions, external)
    marker = _data_dir() / MARKER
    _atomic_json(marker, {"version": 1, "started_at": _now(), "source": str(directory), "state": "restoring"})
    _write_ledger(deletions)
    posts.close_db()
    source_db = sqlite3.connect(directory / "blog.sqlite3")
    target = sqlite3.connect(current_app.config["DATABASE"])
    try:
        source_db.backup(target)
    finally:
        target.close()
        source_db.close()
    for name in manifest["files"]:
        if name == "blog.sqlite3":
            continue
        path = _data_dir().joinpath(*PurePosixPath(name).parts)
        if path.is_symlink() or _data_dir() not in path.resolve().parents:
            raise ValueError("Restore destination must stay inside the configured data directory.")
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + "." + uuid4().hex + ".restore")
        try:
            shutil.copyfile(directory.joinpath(*PurePosixPath(name).parts), temporary)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    posts.init_db()
    import auth
    import newsletter
    import media
    auth.init_schema()
    newsletter.init_schema()
    db = posts.get_db()
    try:
        db.execute("BEGIN IMMEDIATE")
        candidates = set()
        removed = 0
        for slug, deleted_at in deletions.items():
            row = db.execute("SELECT id, body FROM posts WHERE slug = ?", (slug,)).fetchone()
            if row:
                candidates.update(image_ids(json.loads(row["body"])))
                edit = db.execute("SELECT body FROM post_edits WHERE post_id = ?", (row["id"],)).fetchone()
                if edit:
                    candidates.update(image_ids(json.loads(edit["body"])))
                newsletter.delete_post_notifications(row["id"], db=db)
                db.execute("DELETE FROM posts WHERE id = ?", (row["id"],))
                removed += 1
            db.execute("INSERT OR REPLACE INTO deleted_posts VALUES (?, ?)", (slug, deleted_at))
        for table in ("admin_sessions", "tab_unlocks", "navigation_tickets", "login_attempts"):
            db.execute("DELETE FROM " + table)
        db.execute("""UPDATE email_jobs SET status='skipped',last_error='Restored backup: explicit review required',
                      lease_until=NULL,lease_token=NULL WHERE status NOT IN ('sent','skipped')""")
        db.execute("UPDATE notification_events SET status='skipped' WHERE status NOT IN ('sent','skipped')")
        media.enqueue_orphan_media(candidates)
        db.commit()
    except Exception:
        db.rollback()
        raise
    cleanup = media.cleanup_media()
    summary = _validate_database(db, _data_dir() / "uploads")
    _atomic_json(marker, {"version": 1, "started_at": _now(), "source": str(directory),
                          "state": "review_required", "removed_deleted_posts": removed, **summary})
    return {"held": True, "removed_deleted_posts": removed, "cleanup": cleanup, **summary}


def review_restored_data(*, approve=False):
    """Check integrity, deletion history and samples before explicit release."""
    db = posts.get_db()
    summary = _validate_database(db, _data_dir() / "uploads")
    sidecar = _data_dir() / LEDGER
    deletions = _merge_deletions(_db_deletions(db), _read_ledger(sidecar) if sidecar.exists() else {})
    for slug in deletions:
        if db.execute("SELECT 1 FROM posts WHERE slug = ?", (slug,)).fetchone():
            raise ValueError("A removed permalink reappeared; do not expose this restored backup: " + slug)
    if production_samples_present():
        raise ValueError("Production contains sample posts; remove them before approval.")
    marker = _data_dir() / MARKER
    if approve:
        if marker.exists():
            state = json.loads(marker.read_text(encoding="utf-8"))
            if not isinstance(state, dict) or state.get("state") != "review_required":
                raise ValueError("Restore did not finish. Retry restore-data successfully before approval.")
        if summary["pending_cleanup"]:
            raise ValueError("Media cleanup is still pending; run cleanup-media before approval.")
        persist_deletion_ledger()
        marker.unlink(missing_ok=True)
    return {"verified": True, "held": marker.exists(), "approved": bool(approve), **summary}


def production_samples_present():
    """Recognize placeholders without banning real writing's stable URLs."""
    if not current_app.config.get('PRODUCTION'):
        return False
    from sample_posts import SAMPLE_POSTS
    samples = {sample['slug']: sample for sample in SAMPLE_POSTS}
    rows = posts.get_db().execute(
        "SELECT slug,title,body FROM posts WHERE slug IN (?,?,?) OR lower(title) LIKE 'sample article:%' OR lower(title) LIKE 'sample draft:%'",
        tuple(samples),
    )
    for row in rows:
        if row['title'].lower().startswith(('sample article:', 'sample draft:')):
            return True
        sample = samples.get(row['slug'])
        if sample and json.loads(row['body']) == sample['body']:
            return True
    return False


def init_app(app):
    @app.cli.command("backup-data")
    @click.option("--output", required=True, type=click.Path(path_type=Path))
    def backup_command(output):
        """Create a new private, checksum-verified SQLite/media backup."""
        _command(lambda: backup_data(output))

    @app.cli.command("restore-data")
    @click.option("--source", required=True, type=click.Path(exists=True, file_okay=False, path_type=Path))
    @click.option("--deletions", "deletions_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
    def restore_command(source, deletions_path):
        """Restore privately; keep all routes held until explicit review."""
        _command(lambda: restore_data(source, deletions_path=deletions_path))

    @app.cli.command("review-restored-data")
    @click.option("--approve", is_flag=True, help="Remove the restore hold only after all validation passes.")
    def review_command(approve):
        """Validate restored content and optionally release the restore hold."""
        _command(lambda: review_restored_data(approve=approve))


def _command(work):
    try:
        click.echo(json.dumps(work(), ensure_ascii=False))
    except (OSError, ValueError, sqlite3.Error) as error:
        raise click.ClickException(str(error)) from error
