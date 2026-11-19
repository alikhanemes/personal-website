"""One local admin, revocable SQLite sessions, CSRF and per-tab unlocks."""

from functools import wraps
import hashlib
import json
from pathlib import Path
import secrets
import time
from urllib.parse import urlsplit

import click
from flask import abort, current_app, g, jsonify, request
from werkzeug.security import check_password_hash, generate_password_hash

from posts import get_db

COOKIE = "blog_session"


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def init_schema():
    get_db().executescript("""
        CREATE TABLE IF NOT EXISTS admin_sessions (
            token_hash TEXT PRIMARY KEY, csrf TEXT NOT NULL,
            expires_at REAL NOT NULL, authenticated INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS tab_unlocks (
            session_hash TEXT NOT NULL, tab_id TEXT NOT NULL,
            PRIMARY KEY (session_hash, tab_id)
        );
        CREATE TABLE IF NOT EXISTS navigation_tickets (
            ticket_hash TEXT PRIMARY KEY, session_hash TEXT NOT NULL,
            tab_id TEXT NOT NULL, expires_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS login_attempts (address TEXT NOT NULL, attempted_at REAL NOT NULL);
    """)


def load_session():
    g.admin_session = None
    cookie = request.cookies.get(COOKIE, "")
    if cookie and len(cookie) < 200:
        row = get_db().execute("SELECT * FROM admin_sessions WHERE token_hash = ? AND expires_at > ?", (digest(cookie), time.time())).fetchone()
        if row:
            g.admin_session = dict(row)


def new_session(authenticated=False):
    token = secrets.token_urlsafe(32)
    row = dict(token_hash=digest(token), csrf=secrets.token_urlsafe(32),
               expires_at=time.time() + current_app.config["SESSION_TTL"], authenticated=int(authenticated))
    db = get_db()
    db.execute("INSERT INTO admin_sessions VALUES (:token_hash,:csrf,:expires_at,:authenticated)", row)
    db.commit()
    g.admin_session = row
    g.new_session_cookie = token
    return row


def tab_id():
    value = request.headers.get("X-Tab-Token") or request.args.get("tab", "")
    return value if isinstance(value, str) and 20 <= len(value) <= 100 and all(c.isalnum() or c in "-_" for c in value) else ""


def unlocked():
    session = getattr(g, "admin_session", None)
    return bool(session and session["authenticated"] and tab_id() and get_db().execute(
        "SELECT 1 FROM tab_unlocks WHERE session_hash = ? AND tab_id = ?", (session["token_hash"], tab_id())
    ).fetchone())


def check_csrf():
    session = getattr(g, "admin_session", None)
    token = request.headers.get("X-CSRF-Token") or request.form.get("csrf", "")
    if not session or not secrets.compare_digest(session["csrf"], token):
        abort(403, "Refresh this page before trying again.")
    origin = request.headers.get("Origin")
    if origin and (urlsplit(origin).netloc != request.host or urlsplit(origin).scheme != request.scheme):
        abort(403, "Cross-origin request rejected.")
    if request.headers.get("Sec-Fetch-Site") == "cross-site":
        abort(403)


def private(view):
    @wraps(view)
    def guarded(*args, **kwargs):
        if not unlocked():
            return jsonify(error="Verify this tab at /shygarma before continuing."), 401
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            check_csrf()
        return view(*args, **kwargs)
    return guarded


def admin_hash():
    configured = current_app.config.get("ADMIN_PASSWORD_HASH")
    if configured:
        return configured
    path = Path(current_app.config["DATA_DIR"]) / "admin.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))["password_hash"]
    except (OSError, ValueError, KeyError, TypeError):
        return None


def revoke_all():
    db = get_db()
    for table in ("admin_sessions", "tab_unlocks", "navigation_tickets"):
        db.execute(f"DELETE FROM {table}")
    db.commit()


def login(password, binding):
    check_csrf()
    if not isinstance(binding, str) or not 20 <= len(binding) <= 100 or not all(c.isalnum() or c in "-_" for c in binding):
        abort(400, "Invalid tab binding.")
    password_hash = admin_hash()
    if not password_hash:
        return jsonify(error="Run the set-admin CLI command first."), 503
    address = request.remote_addr or "local"
    now = time.time()
    db = get_db()
    db.execute("BEGIN IMMEDIATE")
    db.execute("DELETE FROM login_attempts WHERE attempted_at < ?", (now - current_app.config["LOGIN_WINDOW"],))
    attempts = db.execute("SELECT COUNT(*) FROM login_attempts WHERE address = ?", (address,)).fetchone()[0]
    if attempts >= current_app.config["LOGIN_LIMIT"]:
        db.commit()
        response = jsonify(error="Too many attempts. Try again in 15 minutes.")
        response.headers["Retry-After"] = str(current_app.config["LOGIN_WINDOW"])
        return response, 429
    db.execute("INSERT INTO login_attempts VALUES (?,?)", (address, now))
    db.commit()
    if not isinstance(password, str) or len(password) > 1024 or not check_password_hash(password_hash, password):
        return jsonify(error="Password was not accepted."), 401
    if not g.admin_session["authenticated"]:
        previous = g.admin_session["token_hash"]
        new_session(authenticated=True)
        db.execute("DELETE FROM admin_sessions WHERE token_hash = ?", (previous,))
    db.execute("INSERT OR IGNORE INTO tab_unlocks VALUES (?,?)", (g.admin_session["token_hash"], binding))
    db.execute("DELETE FROM login_attempts WHERE address = ?", (address,))
    db.commit()
    return jsonify(csrf=g.admin_session["csrf"], tabToken=binding)


def init_app(app):
    app.before_request(load_session)

    @app.after_request
    def session_headers(response):
        token = getattr(g, "new_session_cookie", None)
        if token:
            response.set_cookie(COOKIE, token, max_age=int(app.config["SESSION_TTL"]),
                                httponly=True, secure=app.config["COOKIE_SECURE"], samesite="Strict", path="/")
        if request.path.startswith("/shygarma") or getattr(g, "admin_session", None):
            response.headers["Cache-Control"] = "no-store, private"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        return response

    @app.cli.command("set-admin")
    def set_admin():
        """Set the one admin password with a hidden prompt; revoke all sessions."""
        password = click.prompt("New admin password", hide_input=True, confirmation_prompt=True)
        if len(password) < 12:
            raise click.ClickException("Use at least 12 characters.")
        path = Path(app.config["DATA_DIR"]) / "admin.json"
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps({"password_hash": generate_password_hash(password, method="scrypt")}), encoding="utf-8")
        temporary.replace(path)
        revoke_all()
        click.echo("Admin password configured. All sessions revoked. Open /shygarma to sign in.")

    @app.cli.command("revoke-sessions")
    def revoke_sessions():
        """Immediately revoke all sign-ins and tab unlocks."""
        revoke_all()
        click.echo("All sessions revoked.")
