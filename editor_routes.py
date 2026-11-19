"""Authorized editor HTTP boundary; public rendering and storage stay independent."""

from pathlib import Path
import secrets
import time

from flask import abort, g, jsonify, render_template, request, send_file

import auth
import media
import posts
import newsletter
from rendering import render_body


def payload():
    value = request.get_json(silent=True)
    if not isinstance(value, dict):
        abort(400, 'A JSON object is required.')
    return value


def register_editor(app):
    @app.get('/shygarma')
    def editor_gate():
        if g.admin_session is None:
            auth.new_session()
        return render_template('signin.html', active=None, csrf=g.admin_session['csrf'],
                               configured=bool(auth.admin_hash()), auth_bridge=False)

    @app.post('/shygarma/login')
    def editor_login():
        data = payload()
        return auth.login(data.get('password'), data.get('tab_id'))

    @app.get('/shygarma/status')
    @auth.private
    def editor_status():
        return jsonify(csrf=g.admin_session['csrf'], tabToken=auth.tab_id())

    @app.post('/shygarma/navigation')
    @auth.private
    def navigation_ticket():
        ticket = secrets.token_urlsafe(32)
        db = posts.get_db()
        db.execute('DELETE FROM navigation_tickets WHERE expires_at < ?', (time.time(),))
        db.execute('INSERT INTO navigation_tickets VALUES (?,?,?,?)',
                   (auth.digest(ticket), g.admin_session['token_hash'], auth.tab_id(), time.time() + 20))
        db.commit()
        return jsonify(ticket=ticket)

    @app.post('/shygarma/resume')
    def resume_navigation():
        auth.check_csrf()
        data = payload()
        token = data.get('ticket', '')
        binding = data.get('tab_id', '')
        if not isinstance(token, str) or not isinstance(binding, str):
            abort(400)
        db = posts.get_db()
        row = db.execute('DELETE FROM navigation_tickets WHERE ticket_hash=? AND session_hash=? AND tab_id=? AND expires_at>? RETURNING tab_id',
                         (auth.digest(token), g.admin_session['token_hash'], binding, time.time())).fetchone()
        db.commit()
        if row is None or not g.admin_session['authenticated']:
            abort(401)
        return jsonify(csrf=g.admin_session['csrf'], tabToken=binding)

    @app.post('/shygarma/logout')
    @auth.private
    def editor_logout():
        db = posts.get_db()
        key = g.admin_session['token_hash']
        db.execute('DELETE FROM tab_unlocks WHERE session_hash=?', (key,))
        db.execute('DELETE FROM navigation_tickets WHERE session_hash=?', (key,))
        db.execute('DELETE FROM admin_sessions WHERE token_hash=?', (key,))
        db.commit()
        response = jsonify(ok=True)
        response.delete_cookie(auth.COOKIE, path='/', httponly=True, samesite='Strict')
        return response

    @app.get('/shygarma/workspace')
    @auth.private
    def editor_workspace():
        config = dict(csrf=g.admin_session['csrf'], tabToken=auth.tab_id(), apiBase='/shygarma/api',
                      post=None, posts=posts.editor_list_posts(), maxImageBytes=media.MAX_UPLOAD_BYTES,
                      maxImagePixels=app.config.get('MAX_IMAGE_PIXELS', media.MAX_IMAGE_PIXELS))
        return render_template('editor.html', editor_config=config, active=None, auth_bridge=True)

    @app.get('/shygarma/assets/editor.js')
    @auth.private
    def editor_js():
        return send_file(Path(app.static_folder) / 'editor.js', mimetype='text/javascript', conditional=False)

    @app.get('/shygarma/assets/editor.css')
    @auth.private
    def editor_css():
        return send_file(Path(app.static_folder) / 'editor.css', mimetype='text/css', conditional=False)

    @app.post('/shygarma/api/posts')
    @auth.private
    def create_draft():
        data = payload()
        # Validate before inserting so a malformed create leaves no orphan draft.
        from rendering import validate_body
        title = data.get('title', 'Untitled draft')
        body = validate_body(data.get('body', {'type':'doc','content':[]}))
        post = posts.new_draft(title)
        if body['content']:
            post = posts.save_edit(post['id'], post['title'], body, post['version'])
        return jsonify(post), 201

    @app.get('/shygarma/api/posts/<int:post_id>')
    @auth.private
    def read_draft(post_id):
        post = posts.get_edit(post_id)
        if post is None:
            abort(404)
        return jsonify(post)

    @app.put('/shygarma/api/posts/<int:post_id>')
    @auth.private
    def save_draft(post_id):
        data = payload()
        return jsonify(posts.save_edit(post_id, data.get('title'), data.get('body'), data.get('version')))

    @app.post('/shygarma/api/posts/<int:post_id>/publish')
    @auth.private
    def publish_draft(post_id):
        data = payload()
        notify = data.get('notify_subscribers', False)
        if type(notify) is not bool:
            abort(400, 'Choose whether to notify subscribers.')
        return jsonify(posts.publish_edit(post_id, data.get('version'), notify_subscribers=notify))

    def require_post(post_id):
        if posts.get_edit(post_id) is None:
            abort(404)

    @app.post('/shygarma/api/posts/<int:post_id>/archive')
    @auth.private
    def archive_post(post_id):
        require_post(post_id)
        return jsonify(posts.archive_post(post_id, payload().get('version')))

    @app.post('/shygarma/api/posts/<int:post_id>/restore')
    @auth.private
    def restore_post(post_id):
        require_post(post_id)
        return jsonify(posts.restore_post(post_id, payload().get('version')))

    @app.delete('/shygarma/api/posts/<int:post_id>')
    @auth.private
    def delete_post(post_id):
        require_post(post_id)
        data = payload()
        return jsonify(posts.permanently_delete_post(post_id, data.get('confirmation'), data.get('version')))

    @app.post('/shygarma/api/media-cleanup')
    @auth.private
    def retry_media_cleanup():
        return jsonify(cleanup=media.cleanup_media())

    @app.get('/shygarma/api/posts/<int:post_id>/notification')
    @auth.private
    def notification_status(post_id):
        require_post(post_id)
        return jsonify(notification=newsletter.notification_status(post_id))

    @app.post('/shygarma/api/posts/<int:post_id>/notification/retry')
    @auth.private
    def retry_notification(post_id):
        require_post(post_id)
        try:
            status = newsletter.retry_post_notification(post_id)
        except newsletter.NewsletterUnavailable as error:
            return jsonify(notification=newsletter.notification_status(post_id), error=str(error)), 503
        # The durable worker sends separately, so a provider timeout cannot
        # occupy an editor request or roll back an article publication.
        return jsonify(notification=status)

    @app.post('/shygarma/api/posts/<int:post_id>/preview')
    @auth.private
    def preview_draft(post_id):
        version = payload().get('version')
        post = posts.get_edit(post_id)
        if post is None:
            abort(404)
        if type(version) is not int or post['version'] != version:
            raise posts.EditConflict('Save and reload before previewing this revision.')
        body = render_body(post['body'], image_resolver=lambda upload_id: '/media/' + upload_id if media.get_upload(upload_id) else None)
        html = render_template('article.html', post=post, body=body, active=None, auth_bridge=False, private_preview=True)
        return jsonify(html=html)

    @app.post('/shygarma/api/uploads')
    @auth.private
    def upload_image():
        file = request.files.get('file')
        if file is None:
            abort(400, 'Choose an image.')
        upload = media.save_upload(file)
        return jsonify({key:upload[key] for key in ('id','mime_type','width','height','size')}), 201

    @app.get('/shygarma/api/media/<upload_id>')
    @auth.private
    def private_media(upload_id):
        upload = media.get_upload(upload_id)
        if upload is None:
            abort(404)
        try:
            return send_file(upload['path'], mimetype=upload['mime_type'], conditional=False)
        except FileNotFoundError:
            abort(404)

    @app.errorhandler(posts.EditConflict)
    def edit_conflict(error):
        return jsonify(error=str(error)), 409

    @app.errorhandler(ValueError)
    def invalid_content(error):
        if request.path.startswith('/shygarma'):
            return jsonify(error=str(error)), 400
        raise error
