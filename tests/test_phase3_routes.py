"""HTTP coverage for email consent and private phase-three lifecycle actions."""

import copy
from pathlib import Path
import re
import shutil
import unittest
from uuid import uuid4

from werkzeug.security import generate_password_hash

from app import create_app
import newsletter
import posts


PASSWORD = 'Phase three isolated test password!'
PASSWORD_HASH = generate_password_hash(PASSWORD, method='scrypt')


class Phase3RouteTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(__file__).resolve().parents[1] / '.qa' / 'tests' / uuid4().hex
        self.directory.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.directory)
        self.calls = []
        self.failure = False

        def capture(payload, key):
            self.calls.append((copy.deepcopy(payload), key))
            if self.failure:
                raise TimeoutError('Simulated provider outage.')
            return {'id': 'captured-' + key}

        self.app = create_app({
            'TESTING': True, 'DATA_DIR': str(self.directory), 'TELEGRAM_URL': '',
            'ADMIN_PASSWORD_HASH': PASSWORD_HASH, 'PRODUCTION': False,
            'PUBLIC_ENABLED': True, 'EMAIL_MODE': 'test',
            'EMAIL_TEST_ADDRESS': 'reader@example.com',
            'EMAIL_FROM': 'news@letters.example.com',
            'PUBLIC_BASE_URL': 'https://blog.example.com', 'EMAIL_TRANSPORT': capture,
        })
        self.client = self.app.test_client()
        self.subscriber = self.app.test_client()

    def form_csrf(self, response):
        markup = response.get_data(as_text=True)
        match = re.search(r'name="csrf" value="([^"]+)"', markup)
        if not match:
            match = re.search(r'name="csrf-token" content="([^"]+)"', markup)
        self.assertIsNotNone(match, markup)
        return match.group(1)

    def login(self):
        csrf = self.form_csrf(self.client.get('/shygarma'))
        response = self.client.post('/shygarma/login', json={'password': PASSWORD, 'tab_id': uuid4().hex}, headers={'X-CSRF-Token': csrf})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        result = response.get_json()
        return {'X-CSRF-Token': result['csrf'], 'X-Tab-Token': result['tabToken']}

    def create(self, headers, title='Disposable phase three article'):
        response = self.client.post('/shygarma/api/posts', json={
            'title': title, 'body': {'type': 'doc', 'content': [{'type': 'paragraph', 'content': [{'type': 'text', 'text': 'Complete public article body.'}]}]},
        }, headers=headers)
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        return response.get_json()

    def publish(self, post, headers, notify=True):
        response = self.client.post(f"/shygarma/api/posts/{post['id']}/publish", json={'version': post['version'], 'notify_subscribers': notify}, headers=headers)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.get_json()

    def signup_link(self):
        csrf = self.form_csrf(self.subscriber.get('/subscribe'))
        response = self.subscriber.post('/subscribe', data={'csrf': csrf, 'email': 'reader@example.com'})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        with self.app.app_context():
            self.assertEqual(newsletter.process_outbox()['sent'], 1)
        return re.search(r'(https://blog\.example\.com)(/subscribe/confirm/[A-Za-z0-9_-]+)', self.calls[-1][0]['text']).group(2)

    def join(self):
        path = self.signup_link()
        csrf = self.form_csrf(self.subscriber.get(path))
        response = self.subscriber.post(path, data={'csrf': csrf})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return path

    def status(self, post, headers):
        response = self.client.get(f"/shygarma/api/posts/{post['id']}/notification", headers=headers)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.get_json()['notification']

    def test_signup_csrf_confirmation_scanner_safety_and_no_public_caching(self):
        response = self.subscriber.get('/subscribe')
        csrf = self.form_csrf(response)
        self.assertIn('no-store', response.headers['Cache-Control'])
        self.assertIn('noindex', response.headers['X-Robots-Tag'])
        self.assertEqual(self.subscriber.post('/subscribe', data={'email': 'reader@example.com'}).status_code, 403)
        self.assertEqual(self.subscriber.post('/subscribe', data={'csrf': csrf, 'email': 'reader@example.com'}, headers={'Origin': 'https://unrelated.example'}).status_code, 403)
        path = self.signup_link()
        response = self.subscriber.get(path)
        csrf = self.form_csrf(response)
        self.assertEqual(response.status_code, 200)
        self.assertIn('no-store', response.headers['Cache-Control'])
        self.assertIn('noindex', response.headers['X-Robots-Tag'])
        with self.app.app_context():
            self.assertIsNone(posts.get_db().execute('SELECT confirmed_at FROM subscriptions').fetchone()[0])
        self.assertEqual(self.subscriber.post(path).status_code, 403)
        response = self.subscriber.post(path, data={'csrf': csrf})
        self.assertEqual(response.status_code, 200)
        with self.app.app_context():
            self.assertIsNotNone(posts.get_db().execute('SELECT confirmed_at FROM subscriptions').fetchone()[0])

    def test_unsubscribe_get_is_safe_and_provider_one_click_post_needs_no_cookie(self):
        self.join()
        headers = self.login()
        post = self.publish(self.create(headers), headers)
        with self.app.app_context():
            newsletter.process_outbox()
        path = re.search(r'/unsubscribe/[A-Za-z0-9_-]+', self.calls[-1][0]['text']).group(0)
        scanner = self.app.test_client()
        self.assertEqual(scanner.get(path).status_code, 200)
        with self.app.app_context():
            self.assertIsNone(posts.get_db().execute('SELECT unsubscribed_at FROM subscriptions').fetchone()[0])
        response = scanner.post(path, data={'List-Unsubscribe': 'One-Click'})
        self.assertEqual(response.status_code, 200)
        with self.app.app_context():
            self.assertIsNotNone(posts.get_db().execute('SELECT unsubscribed_at FROM subscriptions').fetchone()[0])
        other = self.publish(self.create(headers, 'After unsubscribe'), headers)
        self.assertEqual(self.status(other, headers)['status'], 'no_confirmed_recipients')
        self.assertEqual(scanner.post('/unsubscribe/not-a-valid-capability').status_code, 404)

    def test_notification_choice_is_strict_and_unconfirmed_addresses_are_excluded(self):
        self.signup_link()
        headers = self.login()
        draft = self.create(headers)
        path = f"/shygarma/api/posts/{draft['id']}/publish"
        response = self.client.post(path, json={'version': draft['version'], 'notify_subscribers': 'yes'}, headers=headers)
        self.assertEqual(response.status_code, 400)
        post = self.publish(draft, headers)
        self.assertEqual(self.status(post, headers)['status'], 'no_confirmed_recipients')
        self.assertIn(post['title'], self.client.get('/').get_data(as_text=True))
        self.assertEqual(self.client.get('/posts/' + post['slug']).status_code, 200)

    def test_lifecycle_and_notification_routes_require_admin_current_tab_and_csrf(self):
        self.join()
        headers = self.login()
        post = self.publish(self.create(headers), headers)
        base = f"/shygarma/api/posts/{post['id']}"
        anonymous = self.app.test_client()
        self.assertEqual(anonymous.get(base + '/notification').status_code, 401)
        self.assertEqual(self.client.get(base + '/notification').status_code, 401)
        for suffix in ('/archive', '/restore', '/notification/retry'):
            with self.subTest(suffix=suffix):
                self.assertEqual(anonymous.post(base + suffix, json={'version': post['version']}).status_code, 401)
                self.assertEqual(self.client.post(base + suffix, json={'version': post['version']}, headers={'X-Tab-Token': headers['X-Tab-Token']}).status_code, 403)
        self.assertEqual(anonymous.delete(base, json={'confirmation': 'DELETE', 'version': post['version']}).status_code, 401)
        self.assertEqual(self.client.delete(base, json={'confirmation': 'DELETE', 'version': post['version']}, headers={'X-Tab-Token': headers['X-Tab-Token']}).status_code, 403)
        self.assertEqual(self.status(post, headers)['status'], 'queued')

    def test_publish_outage_retry_and_update_do_not_create_duplicate_notices(self):
        self.join()
        headers = self.login()
        before = len(self.calls)
        post = self.publish(self.create(headers), headers)
        self.assertEqual(len(self.calls), before, 'Publication must not make a provider call.')
        self.failure = True
        with self.app.app_context():
            self.assertEqual(newsletter.process_outbox()['retry'], 1)
        first_attempt = self.calls[-1]
        self.assertEqual(self.status(post, headers)['status'], 'retry')
        path = f"/shygarma/api/posts/{post['id']}/notification/retry"
        before = len(self.calls)
        self.assertEqual(self.client.post(path, json={}, headers=headers).status_code, 200)
        self.assertEqual(len(self.calls), before, 'Retry endpoint queues work instead of sending synchronously.')
        self.failure = False
        with self.app.app_context():
            self.assertEqual(newsletter.process_outbox()['sent'], 1)
        self.assertEqual(self.calls[-1], first_attempt)
        response = self.client.put(f"/shygarma/api/posts/{post['id']}", json={'title': 'Explicitly updated', 'body': post['body'], 'version': post['version']}, headers=headers)
        self.assertEqual(response.status_code, 200)
        updated = self.publish(response.get_json(), headers, notify=True)
        self.assertEqual(updated['published_at'], post['published_at'])
        self.assertEqual(updated['slug'], post['slug'])
        with self.app.app_context():
            self.assertEqual(newsletter.process_outbox()['sent'], 0)
            self.assertEqual(posts.get_db().execute('SELECT COUNT(*) FROM email_jobs WHERE post_id=?', (post['id'],)).fetchone()[0], 1)

    def test_archive_restore_and_delete_cancel_pending_notices_and_remove_old_urls(self):
        self.join()
        headers = self.login()
        post = self.publish(self.create(headers), headers)
        permalink = '/posts/' + post['slug']
        base = f"/shygarma/api/posts/{post['id']}"
        response = self.client.post(base + '/archive', json={'version': post['version']}, headers=headers)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        archived = response.get_json()
        self.assertEqual(self.client.get(permalink).status_code, 404)
        self.assertNotIn(post['title'], self.client.get('/').get_data(as_text=True))
        self.assertEqual(self.status(archived, headers)['status'], 'skipped')
        before = len(self.calls)
        with self.app.app_context():
            newsletter.process_outbox()
        self.assertEqual(len(self.calls), before)
        response = self.client.post(base + '/restore', json={'version': archived['version']}, headers=headers)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        restored = response.get_json()
        self.assertEqual(restored['slug'], post['slug'])
        self.assertEqual(restored['published_at'], post['published_at'])
        self.assertEqual(self.client.get(permalink).status_code, 200)
        with self.app.app_context():
            self.assertEqual(newsletter.process_outbox()['sent'], 0)
        self.assertEqual(self.client.delete(base, json={'confirmation': 'delete', 'version': restored['version']}, headers=headers).status_code, 400)
        response = self.client.delete(base, json={'confirmation': 'DELETE', 'version': restored['version']}, headers=headers)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(self.client.get(permalink).status_code, 404)
        self.assertEqual(self.client.get(base + '/notification', headers=headers).status_code, 404)
        with self.app.app_context():
            self.assertEqual(posts.get_db().execute('SELECT COUNT(*) FROM email_jobs WHERE post_id=?', (post['id'],)).fetchone()[0], 0)
            self.assertEqual(posts.get_db().execute('SELECT COUNT(*) FROM notification_events WHERE post_id=?', (post['id'],)).fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
