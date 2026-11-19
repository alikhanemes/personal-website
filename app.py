"""Application setup and public routes; private workflow lives in editor_routes."""

import os
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, abort, render_template, request, send_file, g
from werkzeug.middleware.proxy_fix import ProxyFix

import posts
import auth
import media
import newsletter
import operations
from rendering import render_body


def channel_url(value):
    """Only explicit, ordinary web URLs can activate the Telegram control."""
    if not isinstance(value, str) or any(c.isspace() or ord(c) < 32 for c in value):
        return None
    if "\\" in value:
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() in {"http", "https"} and parsed.hostname and not parsed.username:
            return value
    except ValueError:
        pass
    return None


def create_app(test_config=None):
    app = Flask(__name__)
    production = os.environ.get('BLOG_ENV', '').lower() == 'production'
    app.config.from_mapping(
        DATA_DIR=os.environ.get("BLOG_DATA_DIR", str(Path(app.root_path) / "data")),
        TELEGRAM_URL=os.environ.get("BLOG_TELEGRAM_URL", "https://t.me/+ncnRjIBCYVA2N2Yy"),
        SESSION_TTL=12 * 60 * 60,
        LOGIN_LIMIT=5,
        LOGIN_WINDOW=15 * 60,
        PRODUCTION=production,
        PUBLIC_ENABLED=os.environ.get('BLOG_PUBLIC_ENABLED', 'false' if production else 'true').lower() == 'true',
        COOKIE_SECURE=production,
        EMAIL_MODE=os.environ.get('BLOG_EMAIL_MODE', 'disabled'),
        EMAIL_TEST_ADDRESS=os.environ.get('BLOG_EMAIL_TEST_ADDRESS', ''),
        EMAIL_FROM=os.environ.get('BLOG_EMAIL_FROM', ''),
        PUBLIC_BASE_URL=os.environ.get('BLOG_PUBLIC_BASE_URL', ''),
        RESEND_API_KEY=os.environ.get('RESEND_API_KEY', ''),
        MAX_CONTENT_LENGTH=9 * 1024 * 1024,
    )
    if test_config:
        app.config.update(test_config)
    if app.config['PRODUCTION']:
        app.config['COOKIE_SECURE'] = True
        # The production host terminates HTTPS at its trusted proxy.
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=0, x_port=0)
    data_dir = Path(app.config["DATA_DIR"]).expanduser()
    if not data_dir.is_absolute():
        data_dir = Path(app.root_path) / data_dir
    data_dir = data_dir.resolve()
    static_dir = Path(app.static_folder).resolve()
    if data_dir == static_dir or static_dir in data_dir.parents:
        raise ValueError("DATA_DIR must be outside static/.")
    data_dir.mkdir(parents=True, exist_ok=True)
    app.config["DATA_DIR"] = str(data_dir)
    app.config["DATABASE"] = str(data_dir / "blog.sqlite3")
    posts.init_app(app)
    with app.app_context():
        posts.init_db()
        auth.init_schema()
        newsletter.init_schema()
    auth.init_app(app)
    newsletter.init_app(app)
    operations.init_app(app)

    @app.before_request
    def restore_and_production_guard():
        if request.path == '/healthz':
            return None
        if (data_dir / 'restore-review-required.json').exists():
            return render_template('unavailable.html', message='A restored backup is awaiting private review.'), 503
        # A new deployment stays closed until its private data has been checked.
        # Admin setup and review use the host's private management flow.
        if not app.config['PUBLIC_ENABLED']:
            return render_template('unavailable.html', message='The site is being prepared.'), 503
        if operations.production_samples_present():
            return render_template('unavailable.html', message='Production content needs private review.'), 503

    @app.after_request
    def fresh_content(response):
        # No site-controlled HTML/media cache can retain a withdrawn article.
        # Static code/styles contain no article content and remain cacheable.
        if not request.path.startswith('/static/') or response.status_code >= 400:
            response.headers['Cache-Control'] = 'no-store, private' if request.path.startswith('/shygarma') else 'no-store'
        return response

    @app.get('/healthz')
    def health():
        posts.get_db().execute('SELECT 1').fetchone()
        return {'status': 'ok'}

    @app.before_request
    def protect_editor_assets():
        if request.path.startswith('/static/editor.'):
            abort(404)

    @app.context_processor
    def header_config():
        session = getattr(g, 'admin_session', None)
        return {"telegram_url": channel_url(app.config["TELEGRAM_URL"]),
                "auth_bridge": bool(session and session['authenticated']),
                "auth_csrf": session['csrf'] if session else ''}

    def article_body(post):
        return render_body(post['body'], image_resolver=lambda upload_id: '/media/' + upload_id if media.public_upload(upload_id) else None)

    @app.get("/")
    def main():
        post = posts.latest_published()
        return render_template(
            "article.html", post=post, body=article_body(post) if post else "", active="main"
        )

    @app.get("/posts")
    def archive():
        return render_template("posts.html", posts=posts.older_published(), active="posts")

    @app.get("/posts/<slug>")
    def article(slug):
        post = posts.published_by_slug(slug)
        if post is None:
            abort(404)
        return render_template("article.html", post=post, body=article_body(post), active=None)

    @app.get('/media/<upload_id>')
    def public_media(upload_id):
        if not media.public_upload(upload_id):
            abort(404)
        upload = media.get_upload(upload_id)
        if upload is None:
            abort(404)
        try:
            response = send_file(upload['path'], mimetype=upload['mime_type'], conditional=False)
        except FileNotFoundError:
            abort(404)
        response.headers['Cache-Control'] = 'no-store'
        return response

    from editor_routes import register_editor
    register_editor(app)
    from subscription_routes import register_subscriptions
    register_subscriptions(app)

    @app.errorhandler(404)
    def not_found(error):
        return render_template("404.html", active=None), 404

    return app


if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=5000, debug=False)
