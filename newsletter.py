"""Double opt-in and a durable, deliberately conservative Resend outbox.

The confirmed local SQLite subscription table is the newsletter audience. Resend
is only a delivery provider: unconfirmed addresses never receive post notices.
No network request is made during publication. Jobs contain immutable payloads
and stable provider idempotency keys; ambiguous requests older than 23 hours
require manual provider reconciliation, rather than risking a duplicate email.
"""

import hashlib
import html
import json
from pathlib import Path
import re
import secrets
import time
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen
from uuid import uuid4

import click
from flask import current_app

from posts import get_db


CONFIRM_TTL = 48 * 60 * 60
UNSUBSCRIBE_TTL = 10 * 366 * 24 * 60 * 60
SAFE_RETRY_WINDOW = 23 * 60 * 60
LEASE_SECONDS = 90


class NewsletterUnavailable(ValueError):
    """Delivery is disabled, restricted, or has incomplete configuration."""


class SignupThrottled(ValueError):
    retry_after = 3600


def init_schema():
    get_db().executescript("""
        CREATE TABLE IF NOT EXISTS subscriptions (
            id INTEGER PRIMARY KEY, email TEXT NOT NULL UNIQUE,
            created_at REAL NOT NULL, confirmed_at REAL, unsubscribed_at REAL
        );
        CREATE TABLE IF NOT EXISTS subscription_tokens (
            token_hash TEXT PRIMARY KEY,
            subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
            purpose TEXT NOT NULL CHECK(purpose IN ('confirm','unsubscribe')),
            expires_at REAL NOT NULL, used_at REAL
        );
        CREATE TABLE IF NOT EXISTS signup_attempts (
            address_hash TEXT NOT NULL, attempted_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS signup_attempts_address ON signup_attempts(address_hash,attempted_at);
        CREATE TABLE IF NOT EXISTS notification_events (
            post_id INTEGER PRIMARY KEY REFERENCES posts(id) ON DELETE CASCADE,
            notify_requested INTEGER NOT NULL, status TEXT NOT NULL,
            created_at REAL NOT NULL, snapshot_title TEXT, snapshot_slug TEXT
        );
        CREATE TABLE IF NOT EXISTS email_jobs (
            id TEXT PRIMARY KEY,
            post_id INTEGER REFERENCES posts(id) ON DELETE CASCADE,
            subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
            kind TEXT NOT NULL CHECK(kind IN ('confirm','post')),
            status TEXT NOT NULL DEFAULT 'queued',
            payload TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE,
            created_at REAL NOT NULL, available_at REAL NOT NULL,
            first_attempt_at REAL, attempts INTEGER NOT NULL DEFAULT 0,
            lease_until REAL, lease_token TEXT,
            provider_id TEXT, sent_at REAL, last_error TEXT,
            UNIQUE(post_id,subscription_id,kind)
        );
        CREATE INDEX IF NOT EXISTS email_jobs_pending ON email_jobs(status,available_at);
    """)
    # Existing installations may already have an earlier outbox schema.
    columns = {row['name'] for row in get_db().execute('PRAGMA table_info(notification_events)')}
    for name in ('snapshot_title', 'snapshot_slug'):
        if name not in columns:
            get_db().execute('ALTER TABLE notification_events ADD COLUMN ' + name + ' TEXT')
    get_db().commit()


def _digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def normalize_email(value):
    if not isinstance(value, str):
        raise ValueError('Enter a valid email address.')
    value = value.strip().lower()
    # Deliberately reject display names, control characters, and SMTPUTF8. This
    # accepts ordinary mailbox addresses without creating email header syntax.
    if (len(value) > 254 or not value.isascii() or
            not re.fullmatch(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.[a-z]{2,63}", value)):
        raise ValueError('Enter a valid email address.')
    local, domain = value.rsplit('@', 1)
    if len(local) > 64 or local.startswith('.') or local.endswith('.') or '..' in value:
        raise ValueError('Enter a valid email address.')
    if any(not label or len(label) > 63 or label.startswith('-') or label.endswith('-') for label in domain.split('.')):
        raise ValueError('Enter a valid email address.')
    return value


def _base_url():
    try:
        value = current_app.config.get('PUBLIC_BASE_URL', '').rstrip('/')
        parsed = urlsplit(value)
        # Accessing .port also rejects malformed or out-of-range port syntax.
        port = parsed.port
        local = parsed.hostname in {'127.0.0.1', 'localhost', '::1'}
        insecure_local = (current_app.testing or current_app.config.get('EMAIL_MODE') == 'test') and local
        if (not parsed.hostname or parsed.username or parsed.password or parsed.query or
                parsed.fragment or parsed.path or '\\' in value or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value) or
                (parsed.scheme != 'https' and not (parsed.scheme == 'http' and insecure_local))):
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise NewsletterUnavailable('Configure PUBLIC_BASE_URL as the canonical HTTPS site URL.') from None
    return value


def _configuration():
    mode = current_app.config.get('EMAIL_MODE', 'disabled')
    if mode not in {'test', 'live'}:
        raise NewsletterUnavailable('Email subscriptions are not configured yet.')
    base = _base_url()
    sender = current_app.config.get('EMAIL_FROM', '')
    if (not isinstance(sender, str) or not sender or any(ord(c) < 32 or ord(c) == 127 for c in sender) or len(sender) > 300):
        raise NewsletterUnavailable('Configure a verified Resend EMAIL_FROM address.')
    # Display-name mailboxes are allowed for From, but validate their mailbox.
    mailbox = sender.rsplit('<', 1)[-1].removesuffix('>').strip()
    try:
        normalize_email(mailbox)
    except ValueError:
        raise NewsletterUnavailable('Configure a verified Resend EMAIL_FROM address.') from None
    if not current_app.config.get('RESEND_API_KEY') and not callable(current_app.config.get('EMAIL_TRANSPORT')):
        raise NewsletterUnavailable('Configure the Resend API key in host secrets.')
    test_address = None
    if mode == 'test':
        try:
            test_address = normalize_email(current_app.config.get('EMAIL_TEST_ADDRESS'))
        except ValueError:
            raise NewsletterUnavailable('Set EMAIL_TEST_ADDRESS to the test address you explicitly choose.') from None
    return {'mode': mode, 'base': base, 'sender': sender, 'test_address': test_address}


def configured():
    try:
        _configuration()
        return True
    except NewsletterUnavailable:
        return False


def _new_token(db, subscription_id, purpose, ttl):
    token = secrets.token_urlsafe(36)
    db.execute('INSERT INTO subscription_tokens VALUES (?,?,?,?,NULL)',
               (_digest(token), subscription_id, purpose, time.time() + ttl))
    return token


def token_info(token, purpose):
    if purpose not in {'confirm', 'unsubscribe'} or not isinstance(token, str) or not 30 <= len(token) <= 100:
        return None
    row = get_db().execute('''
        SELECT t.subscription_id,t.purpose,t.expires_at,t.used_at,s.email,s.confirmed_at,s.unsubscribed_at
        FROM subscription_tokens t JOIN subscriptions s ON s.id=t.subscription_id
        WHERE t.token_hash=? AND t.purpose=? AND t.expires_at>?
    ''', (_digest(token), purpose, time.time())).fetchone()
    return dict(row) if row else None


def _job(db, subscription_id, kind, payload, *, post_id=None):
    job_id = uuid4().hex
    now = time.time()
    db.execute('''
        INSERT INTO email_jobs(id,post_id,subscription_id,kind,payload,idempotency_key,created_at,available_at)
        VALUES(?,?,?,?,?,?,?,?)
    ''', (job_id, post_id, subscription_id, kind,
          json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':')),
          'blog-' + job_id, now, now))
    return job_id


def signup(email, remote_addr='local'):
    email = normalize_email(email)
    config = _configuration()
    if config['mode'] == 'test' and email != config['test_address']:
        raise NewsletterUnavailable('Email setup currently accepts only the explicitly configured test address.')
    db = get_db()
    now = time.time()
    address_hash = _digest(str(remote_addr)[:200])
    try:
        db.execute('BEGIN IMMEDIATE')
        db.execute('DELETE FROM signup_attempts WHERE attempted_at<?', (now - 3600,))
        count = db.execute('SELECT COUNT(*) FROM signup_attempts WHERE address_hash=?', (address_hash,)).fetchone()[0]
        if count >= current_app.config.get('SIGNUP_LIMIT', 5):
            raise SignupThrottled('Too many signup attempts. Try again in an hour.')
        db.execute('INSERT INTO signup_attempts VALUES (?,?)', (address_hash, now))
        db.execute('INSERT INTO subscriptions(email,created_at) VALUES(?,?) ON CONFLICT(email) DO NOTHING', (email, now))
        subscriber = db.execute('SELECT * FROM subscriptions WHERE email=?', (email,)).fetchone()
        recent = db.execute("""
            SELECT 1 FROM email_jobs j WHERE j.subscription_id=? AND j.kind='confirm' AND j.created_at>?
              AND EXISTS(SELECT 1 FROM subscription_tokens t WHERE t.subscription_id=j.subscription_id
                AND t.purpose='confirm' AND t.used_at IS NULL AND t.expires_at>?)
            LIMIT 1
        """, (subscriber['id'], now - 3600, now)).fetchone()
        if not (subscriber['confirmed_at'] and not subscriber['unsubscribed_at']) and not recent:
            token = _new_token(db, subscriber['id'], 'confirm', CONFIRM_TTL)
            link = config['base'] + '/subscribe/confirm/' + quote(token, safe='')
            payload = {
                'from': config['sender'], 'to': [email], 'subject': 'Confirm your blog subscription',
                'text': 'Confirm your email subscription:\n\n' + link + '\n\nThis link expires in 48 hours. If you did not sign up, ignore this message.',
                'html': '<p>Confirm your email subscription:</p><p><a href="' + html.escape(link, quote=True) + '">Confirm subscription</a></p><p>This link expires in 48 hours. If you did not sign up, ignore this message.</p>',
            }
            _job(db, subscriber['id'], 'confirm', payload)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {'message': 'Check your inbox for a confirmation link. If already subscribed, no action is needed.'}


def confirm(token):
    db = get_db()
    try:
        db.execute('BEGIN IMMEDIATE')
        info = token_info(token, 'confirm')
        if not info:
            db.rollback()
            return False
        # A consumed old confirmation cannot undo a subsequent unsubscribe.
        if not info['used_at']:
            now = time.time()
            db.execute('UPDATE subscriptions SET confirmed_at=?,unsubscribed_at=NULL WHERE id=?', (now, info['subscription_id']))
            db.execute('UPDATE subscription_tokens SET used_at=? WHERE token_hash=?', (now, _digest(token)))
            # Invalidate other outstanding confirmations, so clicking an older
            # email cannot silently re-subscribe after a later opt-out.
            db.execute("UPDATE subscription_tokens SET used_at=? WHERE subscription_id=? AND purpose='confirm' AND used_at IS NULL",
                       (now, info['subscription_id']))
        db.commit()
    except Exception:
        db.rollback()
        raise
    return True


def unsubscribe(token):
    info = token_info(token, 'unsubscribe')
    if not info:
        return False
    db = get_db()
    now = time.time()
    db.execute('UPDATE subscriptions SET unsubscribed_at=? WHERE id=?', (now, info['subscription_id']))
    db.execute('UPDATE subscription_tokens SET used_at=COALESCE(used_at,?) WHERE token_hash=?', (now, _digest(token)))
    db.execute("UPDATE subscription_tokens SET used_at=COALESCE(used_at,?) WHERE subscription_id=? AND purpose='confirm'", (now, info['subscription_id']))
    db.execute("UPDATE email_jobs SET status='skipped',last_error='Recipient unsubscribed.' WHERE subscription_id=? AND status NOT IN ('sent','skipped')", (info['subscription_id'],))
    db.commit()
    return True


def _is_public(db, post_id):
    columns = {row['name'] for row in db.execute('PRAGMA table_info(posts)')}
    archived = ' AND archived_at IS NULL' if 'archived_at' in columns else ''
    return db.execute('SELECT * FROM posts WHERE id=? AND published_at IS NOT NULL' + archived, (post_id,)).fetchone()


def queue_first_publication(post_id, notify, *, db=None):
    """Called in the publication transaction; never commits or contacts Resend."""
    db = db or get_db()
    if db.execute('SELECT 1 FROM notification_events WHERE post_id=?', (post_id,)).fetchone():
        return
    requested = notify is True
    status = 'queued' if requested else 'not_requested'
    post = _is_public(db, post_id)
    db.execute('''INSERT INTO notification_events(post_id,notify_requested,status,created_at,snapshot_title,snapshot_slug)
                  VALUES(?,?,?,?,?,?)''', (post_id, int(requested), status, time.time(), post['title'] if post else None, post['slug'] if post else None))
    if not requested:
        return
    if not post:
        db.execute("UPDATE notification_events SET status='skipped' WHERE post_id=?", (post_id,))
        return
    try:
        config = _configuration()
    except NewsletterUnavailable:
        db.execute("UPDATE notification_events SET status='configuration_required' WHERE post_id=?", (post_id,))
        return
    event = db.execute('SELECT * FROM notification_events WHERE post_id=?', (post_id,)).fetchone()
    _fanout(db, event, config)


def _fanout(db, event, config):
    post_id = event['post_id']
    # Audience membership is fixed to confirmation before first publication.
    # New subscribers do not receive an old notice after a setup/outage retry.
    audience = db.execute('SELECT id,email FROM subscriptions WHERE confirmed_at IS NOT NULL AND confirmed_at<=? AND unsubscribed_at IS NULL', (event['created_at'],)).fetchall()
    queued = 0
    for subscriber in audience:
        if config['mode'] == 'test' and subscriber['email'] != config['test_address']:
            continue
        token = _new_token(db, subscriber['id'], 'unsubscribe', UNSUBSCRIBE_TTL)
        unsubscribe_link = config['base'] + '/unsubscribe/' + token
        link = config['base'] + '/posts/' + quote(event['snapshot_slug'], safe='')
        title = event['snapshot_title']
        payload = {
            'from': config['sender'], 'to': [subscriber['email']], 'subject': 'New post: ' + ' '.join(title.split()),
            'text': title + '\n\nRead the article: ' + link + '\n\nUnsubscribe: ' + unsubscribe_link,
            'html': '<p>' + html.escape(title) + '</p><p><a href="' + html.escape(link, quote=True) + '">Read the article</a></p><p><a href="' + html.escape(unsubscribe_link, quote=True) + '">Unsubscribe</a></p>',
            'headers': {'List-Unsubscribe': '<' + unsubscribe_link + '>', 'List-Unsubscribe-Post': 'List-Unsubscribe=One-Click'},
        }
        _job(db, subscriber['id'], 'post', payload, post_id=post_id)
        queued += 1
    if not queued:
        db.execute("UPDATE notification_events SET status='no_confirmed_recipients' WHERE post_id=?", (post_id,))
    else:
        db.execute("UPDATE notification_events SET status='queued' WHERE post_id=?", (post_id,))


def retry_post_notification(post_id):
    """Explicitly recover a first-publication notice held for configuration.

    Retries of existing delivery jobs are handled by process_outbox. This never
    creates another event, changes existing payloads, or releases needs_review.
    """
    db = get_db()
    try:
        db.execute('BEGIN IMMEDIATE')
        event = db.execute('SELECT * FROM notification_events WHERE post_id=?', (post_id,)).fetchone()
        if not event or not event['notify_requested'] or event['status'] == 'skipped':
            db.commit()
            return notification_status(post_id)
        if event and event['notify_requested'] and event['status'] == 'configuration_required':
            if not _is_public(db, post_id):
                cancel_post_notifications(post_id, db=db)
                db.execute("UPDATE notification_events SET status='skipped' WHERE post_id=?", (post_id,))
            else:
                _fanout(db, event, _configuration())
        # Administrative retry makes eligible jobs available now, retaining
        # their original payload, key and first-attempt timestamp.
        db.execute("UPDATE email_jobs SET status='retry',available_at=? WHERE post_id=? AND status IN ('retry','failed')", (time.time(), post_id))
        db.commit()
    except Exception:
        db.rollback()
        raise
    return notification_status(post_id)


def cancel_post_notifications(post_id, *, db=None):
    db = db or get_db()
    db.execute("UPDATE email_jobs SET status='skipped',last_error='Article is no longer public.' WHERE post_id=? AND status NOT IN ('sent','skipped')", (post_id,))
    db.execute("UPDATE notification_events SET status='skipped' WHERE post_id=? AND status IN ('queued','configuration_required')", (post_id,))


def delete_post_notifications(post_id, *, db=None):
    db = db or get_db()
    db.execute('DELETE FROM email_jobs WHERE post_id=?', (post_id,))
    db.execute('DELETE FROM notification_events WHERE post_id=?', (post_id,))


def notification_status(post_id):
    db = get_db()
    event = db.execute('SELECT * FROM notification_events WHERE post_id=?', (post_id,)).fetchone()
    if not event:
        return {'status': 'not_requested', 'counts': {}}
    counts = {row['status']: row['n'] for row in db.execute('SELECT status,COUNT(*) AS n FROM email_jobs WHERE post_id=? GROUP BY status', (post_id,))}
    status = event['status']
    if counts:
        if counts.get('needs_review'):
            status = 'needs_review'
        elif counts.get('failed'):
            status = 'failed'
        elif counts.get('retry'):
            status = 'retry'
        elif counts.get('queued') or counts.get('sending'):
            status = 'queued'
        elif counts.get('sent'):
            status = 'sent'
        else:
            status = 'skipped'
    return {'status': status, 'counts': counts, 'notify_requested': bool(event['notify_requested'])}


def _resend(payload, idempotency_key):
    transport = current_app.config.get('EMAIL_TRANSPORT')
    if callable(transport):
        return transport(payload, idempotency_key)
    request = Request('https://api.resend.com/emails',
                      data=json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8'),
                      headers={'Authorization': 'Bearer ' + current_app.config['RESEND_API_KEY'],
                               'Content-Type': 'application/json', 'Idempotency-Key': idempotency_key,
                               'User-Agent': 'alikhan-blog/3'}, method='POST')
    with urlopen(request, timeout=20) as response:
        return json.loads(response.read(1024 * 1024))


def _claim(config=None):
    db = get_db()
    now = time.time()
    try:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute("""
            SELECT * FROM email_jobs WHERE
              (status IN ('queued','retry') AND available_at<=?) OR
              (status='sending' AND lease_until<=?)
            ORDER BY created_at,id LIMIT 1
        """, (now, now)).fetchone()
        if not row:
            db.commit()
            return None
        job = dict(row)
        subscriber = db.execute('SELECT * FROM subscriptions WHERE id=?', (job['subscription_id'],)).fetchone()
        reason = None
        if job['kind'] == 'post' and not _is_public(db, job['post_id']):
            reason = 'Article is no longer public.'
        elif not subscriber or (job['kind'] == 'post' and (subscriber['unsubscribed_at'] or not subscriber['confirmed_at'])):
            reason = 'Recipient is no longer subscribed.'
        elif job['kind'] == 'confirm' and now - job['created_at'] >= CONFIRM_TTL:
            reason = 'Confirmation expired before sending.'
        if reason:
            db.execute("UPDATE email_jobs SET status='skipped',last_error=? WHERE id=?", (reason, job['id']))
            db.commit()
            return {'skipped': True}
        if config and config['mode'] == 'test' and json.loads(job['payload']).get('to') != [config['test_address']]:
            # A recipient held by setup restrictions has never been submitted
            # to the provider: do not start its idempotency retry clock.
            db.execute("UPDATE email_jobs SET status='retry',available_at=?,last_error='Test mode permits only the configured test recipient.' WHERE id=?", (now + 3600, job['id']))
            db.commit()
            return {'skipped': True}
        if job['first_attempt_at'] is not None and now - job['first_attempt_at'] >= SAFE_RETRY_WINDOW:
            db.execute("UPDATE email_jobs SET status='needs_review',last_error='Provider outcome needs review: safe idempotency retry window elapsed.' WHERE id=?", (job['id'],))
            db.commit()
            return {'held': True}
        lease_token = uuid4().hex
        db.execute("""
            UPDATE email_jobs SET status='sending',lease_token=?,lease_until=?,
              first_attempt_at=COALESCE(first_attempt_at,?),attempts=attempts+1 WHERE id=?
        """, (lease_token, now + LEASE_SECONDS, now, job['id']))
        db.commit()
        job['lease_token'] = lease_token
        return job
    except Exception:
        db.rollback()
        raise


def process_outbox(limit=20):
    """Bounded background/CLI worker; outages never roll back publication."""
    result = {'sent': 0, 'retry': 0, 'skipped': 0, 'needs_review': 0, 'failed': 0, 'disabled': 0}
    # This applies to the CLI as well as the background worker. A staging site
    # or a restored backup must never email while its public gate is closed.
    if not _delivery_open():
        result['disabled'] = 1
        return result
    try:
        config = _configuration()
    except NewsletterUnavailable:
        result['disabled'] = 1
        return result
    for _ in range(max(0, min(int(limit), 100))):
        # The gate can close while a previous network call is in flight.
        if not _delivery_open():
            result['disabled'] = 1
            break
        job = _claim(config)
        if not job:
            break
        if job.get('held'):
            result['needs_review'] += 1
            continue
        if job.get('skipped'):
            result['skipped'] += 1
            continue
        db = get_db()
        payload = json.loads(job['payload'])
        if config['mode'] == 'test' and payload.get('to') != [config['test_address']]:
            # Preserve this job for later explicit production operation without
            # rewriting its recipient, key or message during setup.
            db.execute("UPDATE email_jobs SET status='retry',available_at=?,last_error='Test mode permits only the configured test recipient.' WHERE id=? AND lease_token=? AND status='sending'", (time.time() + 3600, job['id'], job['lease_token']))
            db.commit()
            result['skipped'] += 1
            continue
        if not _delivery_open():
            # No provider request has started for this lease. Keep its retry
            # clock and attempt count unchanged when releasing it for review.
            db.execute("""UPDATE email_jobs SET status='retry',available_at=?,first_attempt_at=?,attempts=?,
                          lease_until=NULL,lease_token=NULL,last_error='Delivery held for private review.'
                          WHERE id=? AND lease_token=? AND status='sending'""",
                       (time.time() + 20, job['first_attempt_at'], job['attempts'], job['id'], job['lease_token']))
            db.commit()
            result['disabled'] = 1
            break
        # Re-read after claiming: an archive/delete/unsubscribe may have
        # cancelled this lease before the provider request actually starts.
        if not _still_sendable(job):
            result['skipped'] += 1
            continue
        try:
            outcome = _resend(payload, job['idempotency_key'])
            if not isinstance(outcome, dict) or not isinstance(outcome.get('id'), str) or not outcome['id']:
                raise ValueError('Provider response did not include a message ID.')
        except Exception as error:
            # Never retain exception text: it can include credentials, URLs,
            # recipient addresses or provider response bodies.
            permanent = isinstance(error, HTTPError) and 400 <= error.code < 500 and error.code not in {408, 409, 429}
            state = 'failed' if permanent else 'retry'
            message = 'Provider rejected the request. Check sending configuration.' if permanent else 'Delivery not confirmed. A safe retry is queued.'
            attempts = job['attempts'] + 1
            delay = min(3600, 30 * 2 ** min(attempts, 6))
            db.execute("UPDATE email_jobs SET status=?,available_at=?,last_error=?,lease_until=NULL WHERE id=? AND lease_token=? AND status='sending'", (state, time.time() + delay, message, job['id'], job['lease_token']))
            db.commit()
            result[state] += 1
        else:
            db.execute("UPDATE email_jobs SET status='sent',provider_id=?,sent_at=?,last_error=NULL,lease_until=NULL WHERE id=? AND lease_token=? AND status='sending'", (outcome['id'], time.time(), job['id'], job['lease_token']))
            db.commit()
            result['sent'] += 1
    return result


def _delivery_open():
    from operations import production_samples_present
    return bool(current_app.config.get('PUBLIC_ENABLED', True) and
                not (Path(current_app.config['DATA_DIR']) / 'restore-review-required.json').exists() and
                not production_samples_present())


def _still_sendable(job):
    db = get_db()
    live = db.execute("SELECT 1 FROM email_jobs WHERE id=? AND lease_token=? AND status='sending'",
                      (job['id'], job['lease_token'])).fetchone()
    if not live:
        return False
    subscriber = db.execute('SELECT * FROM subscriptions WHERE id=?', (job['subscription_id'],)).fetchone()
    reason = None
    if job['kind'] == 'post' and not _is_public(db, job['post_id']):
        reason = 'Article is no longer public.'
    elif not subscriber or (job['kind'] == 'post' and (subscriber['unsubscribed_at'] or not subscriber['confirmed_at'])):
        reason = 'Recipient is no longer subscribed.'
    if reason:
        db.execute("UPDATE email_jobs SET status='skipped',last_error=? WHERE id=? AND lease_token=? AND status='sending'",
                   (reason, job['id'], job['lease_token']))
        db.commit()
        return False
    return True


def init_app(app):
    @app.cli.command('process-email')
    @click.option('--limit', default=20, type=click.IntRange(1, 100))
    def process_email(limit):
        """Process queued email safely; disabled until email is configured."""
        click.echo(json.dumps(process_outbox(limit), sort_keys=True))
