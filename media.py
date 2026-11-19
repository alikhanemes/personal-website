"""Validated local images, private until referenced by a published article."""

from io import BytesIO
import json
from pathlib import Path
import re
import warnings
from uuid import uuid4

from flask import current_app
from PIL import Image, ImageOps, UnidentifiedImageError

from posts import get_db
from rendering import image_ids


MAX_UPLOAD_BYTES = 8 * 1024 * 1024
MAX_IMAGE_PIXELS = 24_000_000
_FORMATS = {"PNG": ("png", "image/png"), "JPEG": ("jpg", "image/jpeg"), "WEBP": ("webp", "image/webp")}


def init_media_schema():
    get_db().execute("""
        CREATE TABLE IF NOT EXISTS uploads (
            id TEXT PRIMARY KEY,
            filename TEXT NOT NULL UNIQUE,
            mime_type TEXT NOT NULL,
            width INTEGER NOT NULL,
            height INTEGER NOT NULL,
            size INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )
    """)
    get_db().execute("""
        CREATE TABLE IF NOT EXISTS media_cleanup (
            upload_id TEXT PRIMARY KEY REFERENCES uploads(id) ON DELETE CASCADE,
            filename TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            queued_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )
    """)
    get_db().commit()


def _upload_dir():
    directory = Path(current_app.config["DATA_DIR"]) / "uploads"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def save_upload(file):
    """Check actual image bytes, decode, orient, and reencode without metadata."""
    stream = getattr(file, "stream", file)
    raw = stream.read(MAX_UPLOAD_BYTES + 1)
    if not raw or len(raw) > MAX_UPLOAD_BYTES:
        raise ValueError("Choose an image no larger than 8 MiB.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(raw)) as probe:
                image_format = probe.format
                if image_format not in _FORMATS:
                    raise ValueError("Use a PNG, JPEG, or WebP image. SVG and animated images are not supported.")
                if probe.width * probe.height > MAX_IMAGE_PIXELS:
                    raise ValueError("Images must contain at most 24 megapixels.")
                if getattr(probe, "n_frames", 1) != 1:
                    raise ValueError("Animated images are not supported.")
                probe.verify()
            with Image.open(BytesIO(raw)) as source:
                source.load()
                image = ImageOps.exif_transpose(source)
                if image_format == "JPEG":
                    image = image.convert("RGB")
                elif image.mode not in {"RGB", "RGBA", "L", "LA", "P"}:
                    image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
                encoded = BytesIO()
                options = {"quality": 90} if image_format in {"JPEG", "WEBP"} else {}
                image.save(encoded, format=image_format, **options)
                width, height = image.size
                clean = encoded.getvalue()
    except ValueError:
        raise
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
        raise ValueError("This file is not a valid supported image.") from error
    if len(clean) > MAX_UPLOAD_BYTES:
        raise ValueError("The decoded image is too large. Resize it and try again.")

    upload_id = uuid4().hex
    extension, mime_type = _FORMATS[image_format]
    filename = f"{upload_id}.{extension}"
    path = _upload_dir() / filename
    # Exclusive creation means the random ID can never overwrite an old image.
    with path.open("xb") as output:
        output.write(clean)
    db = get_db()
    try:
        db.execute("INSERT INTO uploads (id, filename, mime_type, width, height, size) VALUES (?, ?, ?, ?, ?, ?)",
                   (upload_id, filename, mime_type, width, height, len(clean)))
        db.commit()
    except Exception:
        db.rollback()
        path.unlink(missing_ok=True)
        raise
    return get_upload(upload_id)


def get_upload(upload_id):
    if not isinstance(upload_id, str) or not re.fullmatch(r"[0-9a-f]{32}", upload_id):
        return None
    row = get_db().execute("""
        SELECT * FROM uploads WHERE id = ?
        AND NOT EXISTS (SELECT 1 FROM media_cleanup WHERE upload_id = uploads.id)
    """, (upload_id,)).fetchone()
    if row is None:
        return None
    upload = dict(row)
    # Do not trust a stored filename as an arbitrary filesystem path.
    if not re.fullmatch(rf"{upload_id}\.(?:png|jpg|webp)", upload["filename"]):
        return None
    path = _upload_dir() / upload["filename"]
    if not path.is_file():
        return None
    upload["path"] = str(path)
    return upload


def public_upload(upload_id):
    """Publication, not upload time or working edits, controls public access."""
    if get_upload(upload_id) is None:
        return False
    for row in get_db().execute("SELECT body FROM posts WHERE published_at IS NOT NULL AND archived_at IS NULL"):
        try:
            body = json.loads(row["body"])
        except (TypeError, ValueError):
            continue
        if upload_id in image_ids(body):
            return True
    return False


def referenced_upload_ids():
    """All public, archived, draft and working documents retain their images."""
    referenced = set()
    for row in get_db().execute("SELECT body FROM posts UNION ALL SELECT body FROM post_edits"):
        try:
            referenced.update(image_ids(json.loads(row["body"])))
        except (TypeError, ValueError):
            # Fail closed if storage is corrupt instead of deleting possible
            # image references that an older/future renderer cannot interpret.
            raise ValueError("An article document is invalid; image cleanup was deferred.")
    return referenced


def enqueue_orphan_media(candidates):
    """Queue affected, unshared images inside the caller's write transaction.

    Restricting the sweep to the deleted article's images avoids deleting a
    newly uploaded image another editor tab has not autosaved into its body yet.
    This function intentionally does not commit.
    """
    referenced = referenced_upload_ids()
    db = get_db()
    for upload_id in set(candidates) - referenced:
        db.execute("""
            INSERT INTO media_cleanup (upload_id, filename)
            SELECT id, filename FROM uploads WHERE id = ?
            ON CONFLICT(upload_id) DO NOTHING
        """, (upload_id,))


def cleanup_media():
    """Retry durable cleanup jobs; a failed unlink never pretends to succeed."""
    db = get_db()
    removed = 0
    errors = []
    # Each file is its own transaction. Holding the writer lock across unlink
    # prevents a simultaneous save from acquiring a reference during removal.
    upload_ids = [row["upload_id"] for row in db.execute("SELECT upload_id FROM media_cleanup")]
    for upload_id in upload_ids:
        try:
            db.execute("BEGIN IMMEDIATE")
            job = db.execute("SELECT * FROM media_cleanup WHERE upload_id = ?", (upload_id,)).fetchone()
            if job is None:
                db.commit()
                continue
            if upload_id in referenced_upload_ids():
                db.execute("DELETE FROM media_cleanup WHERE upload_id = ?", (upload_id,))
                db.commit()
                continue
            if not re.fullmatch(r"[0-9a-f]{32}", upload_id) or not re.fullmatch(rf"{upload_id}\.(?:png|jpg|webp)", job["filename"]):
                raise ValueError("Invalid stored media filename; manual review is required.")
            (_upload_dir() / job["filename"]).unlink(missing_ok=True)
            db.execute("DELETE FROM uploads WHERE id = ?", (upload_id,))
            db.commit()
            removed += 1
        except (OSError, ValueError) as error:
            db.rollback()
            message = str(error)
            db.execute("UPDATE media_cleanup SET attempts = attempts + 1, last_error = ? WHERE upload_id = ?",
                       (message, upload_id))
            db.commit()
            errors.append({"upload_id": upload_id, "error": message})
        except Exception:
            db.rollback()
            raise
    pending = db.execute("SELECT COUNT(*) FROM media_cleanup").fetchone()[0]
    return {"removed": removed, "pending": pending, "failed": len(errors), "errors": errors}
