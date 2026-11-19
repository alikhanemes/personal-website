"""Public double opt-in forms; delivery happens through the durable outbox."""

from flask import abort, g, render_template, request

import auth
import newsletter


def register_subscriptions(app):
    def csrf():
        if g.admin_session is None:
            auth.new_session()
        return g.admin_session['csrf']

    @app.route('/subscribe', methods=['GET', 'POST'])
    def subscribe():
        token = csrf()
        error = message = None
        status = 200
        if request.method == 'POST':
            auth.check_csrf()
            try:
                message = newsletter.signup(request.form.get('email'), request.remote_addr or 'local')['message']
            except newsletter.SignupThrottled as problem:
                error, status = str(problem), 429
            except newsletter.NewsletterUnavailable:
                error, status = 'Email subscriptions are being set up. Please try again later.', 503
            except ValueError as problem:
                error, status = str(problem), 400
        return render_template('subscribe.html', active=None, csrf=token,
                               available=newsletter.configured(), error=error, message=message), status

    @app.route('/subscribe/confirm/<token>', methods=['GET', 'POST'])
    def confirm_subscription(token):
        if newsletter.token_info(token, 'confirm') is None:
            abort(404)
        form_csrf = csrf()
        if request.method == 'POST':
            auth.check_csrf()
            if not newsletter.confirm(token):
                abort(404)
            return render_template('subscription_action.html', active=None, done=True,
                                   title='Subscription confirmed', message='You will receive a short email when a new article is published.')
        return render_template('subscription_action.html', active=None, csrf=form_csrf,
                               title='Confirm your subscription', message='Confirm that you want new-post emails from this blog.', action='Confirm subscription')

    @app.route('/unsubscribe/<token>', methods=['GET', 'POST'])
    def unsubscribe(token):
        if newsletter.token_info(token, 'unsubscribe') is None:
            abort(404)
        if request.method == 'POST':
            # The high-entropy email capability authorizes this action. No
            # cookie/CSRF dependency: mail providers use RFC 8058 one-click POST.
            if not newsletter.unsubscribe(token):
                abort(404)
            return render_template('subscription_action.html', active=None, done=True,
                                   title='You are unsubscribed', message='You will no longer receive new-post emails.')
        return render_template('subscription_action.html', active=None,
                               title='Unsubscribe', message='Stop receiving new-post emails from this blog.', action='Unsubscribe')

    @app.after_request
    def private_email_links(response):
        if request.path.startswith(('/subscribe', '/unsubscribe')):
            response.headers['Cache-Control'] = 'no-store'
            response.headers['X-Robots-Tag'] = 'noindex, nofollow'
        return response
