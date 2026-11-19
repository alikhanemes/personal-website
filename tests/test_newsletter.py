"""Newsletter tests use captured transport only; no messages leave the process."""

import copy
import json
from pathlib import Path
import re
import shutil
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from app import create_app
import newsletter
import posts


class NewsletterTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(__file__).resolve().parents[1] / '.qa' / 'tests' / uuid4().hex
        self.directory.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.directory)
        self.calls = []
        self.failure = False

        def transport(payload, key):
            self.calls.append((copy.deepcopy(payload), key))
            if self.failure:
                raise TimeoutError('Sensitive provider diagnostic is never persisted.')
            return {'id': 'captured-' + key}

        self.app = create_app({
            'TESTING': True, 'DATA_DIR': str(self.directory),
            'EMAIL_MODE': 'test', 'EMAIL_TEST_ADDRESS': 'reader@example.com',
            'EMAIL_FROM': 'Blog <news@letters.example.com>',
            'PUBLIC_BASE_URL': 'https://blog.example.com',
            'EMAIL_TRANSPORT': transport, 'SIGNUP_LIMIT': 50,
        })
        self.context = self.app.app_context()
        self.context.push()
        self.addCleanup(self.context.pop)
        newsletter.init_schema()

    def join(self, email='reader@example.com'):
        newsletter.signup(email)
        result = newsletter.process_outbox()
        self.assertEqual(result['sent'], 1)
        payload = self.calls[-1][0]
        token = re.search(r'/subscribe/confirm/([A-Za-z0-9_-]+)', payload['text']).group(1)
        self.assertTrue(newsletter.confirm(token))
        return token

    def publish(self, title='A new article', notify=True):
        body = {'type': 'doc', 'content': [{'type': 'paragraph', 'content': [{'type': 'text', 'text': 'The article stays on the website.'}]}]}
        posts.create_post('article-' + uuid4().hex, title, body, '2026-11-19T10:00:00Z')
        row = posts.get_db().execute('SELECT * FROM posts ORDER BY id DESC LIMIT 1').fetchone()
        newsletter.queue_first_publication(row['id'], notify)
        posts.get_db().commit()
        return dict(row)

    def jobs(self, post_id=None):
        if post_id is None:
            return posts.get_db().execute('SELECT * FROM email_jobs ORDER BY created_at,id').fetchall()
        return posts.get_db().execute('SELECT * FROM email_jobs WHERE post_id=?', (post_id,)).fetchall()

    def test_disabled_by_default_and_test_mode_restricts_exact_recipient(self):
        self.app.config['EMAIL_MODE'] = 'disabled'
        with self.assertRaises(newsletter.NewsletterUnavailable):
            newsletter.signup('reader@example.com')
        self.assertEqual(newsletter.process_outbox()['disabled'], 1)
        self.assertEqual(self.calls, [])
        self.app.config['EMAIL_MODE'] = 'test'
        with self.assertRaises(newsletter.NewsletterUnavailable):
            newsletter.signup('somebody-else@example.com')
        self.app.config['EMAIL_TEST_ADDRESS'] = ''
        with self.assertRaises(newsletter.NewsletterUnavailable):
            newsletter.signup('reader@example.com')

    def test_public_gate_and_restore_hold_prevent_cli_and_worker_delivery(self):
        newsletter.signup('reader@example.com')
        self.app.config['PUBLIC_ENABLED'] = False
        self.assertEqual(newsletter.process_outbox()['disabled'], 1)
        result = self.app.test_cli_runner().invoke(args=['process-email'])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn('"disabled": 1', result.output)
        self.assertEqual(self.calls, [])
        self.app.config['PUBLIC_ENABLED'] = True
        marker = self.directory / 'restore-review-required.json'
        marker.write_text('{}', encoding='utf-8')
        self.assertEqual(newsletter.process_outbox()['disabled'], 1)
        self.assertEqual(self.calls, [])
        marker.unlink()
        self.assertEqual(newsletter.process_outbox()['sent'], 1)

    def test_production_sample_guard_blocks_delivery(self):
        newsletter.signup('reader@example.com')
        posts.create_post('sample-should-never-deploy', 'Sample article: do not deploy', {'type': 'doc', 'content': []})
        self.app.config['PRODUCTION'] = True
        self.assertEqual(newsletter.process_outbox()['disabled'], 1)
        self.assertEqual(self.calls, [])

    def test_confirmation_is_required_and_signup_does_not_duplicate_mail(self):
        newsletter.signup(' Reader@Example.com ')
        newsletter.signup('reader@example.com')
        self.assertEqual(len(self.jobs()), 1)
        post = self.publish()
        self.assertEqual(len(self.jobs(post['id'])), 0)
        self.assertEqual(newsletter.notification_status(post['id'])['status'], 'no_confirmed_recipients')
        self.join()
        confirmed = posts.get_db().execute('SELECT * FROM subscriptions').fetchone()
        self.assertIsNotNone(confirmed['confirmed_at'])
        newsletter.signup('reader@example.com')
        self.assertEqual(len(self.jobs()), 1)

    def test_confirmation_get_is_read_only_and_token_expiry_is_checked(self):
        newsletter.signup('reader@example.com')
        newsletter.process_outbox()
        token = re.search(r'/subscribe/confirm/([A-Za-z0-9_-]+)', self.calls[-1][0]['text']).group(1)
        info = newsletter.token_info(token, 'confirm')
        self.assertEqual(info['email'], 'reader@example.com')
        self.assertIsNone(posts.get_db().execute('SELECT confirmed_at FROM subscriptions').fetchone()[0])
        self.assertIsNone(newsletter.token_info(token, 'unsubscribe'))
        posts.get_db().execute('UPDATE subscription_tokens SET expires_at=0')
        posts.get_db().commit()
        self.assertFalse(newsletter.confirm(token))

    def test_audience_contains_only_confirmed_addresses_and_notice_is_short(self):
        self.join()
        self.app.config['EMAIL_MODE'] = 'live'
        newsletter.signup('unconfirmed@example.com', 'different-address')
        post = self.publish('<Title & punctuation>')
        jobs = self.jobs(post['id'])
        self.assertEqual(len(jobs), 1)
        payload = json.loads(jobs[0]['payload'])
        self.assertEqual(payload['to'], ['reader@example.com'])
        self.assertIn('https://blog.example.com/posts/' + post['slug'], payload['text'])
        self.assertNotIn('The article stays on the website.', payload['text'])
        self.assertIn('&lt;Title &amp; punctuation&gt;', payload['html'])
        self.assertIn('List-Unsubscribe', payload['headers'])
        self.assertEqual(payload['headers']['List-Unsubscribe-Post'], 'List-Unsubscribe=One-Click')

    def test_first_publish_fence_blocks_edits_restores_and_opt_out_requeue(self):
        self.join()
        post = self.publish(notify=True)
        newsletter.queue_first_publication(post['id'], True)
        posts.get_db().commit()
        self.assertEqual(len(self.jobs(post['id'])), 1)
        newsletter.process_outbox()
        before = len(self.calls)
        newsletter.queue_first_publication(post['id'], True)
        posts.get_db().commit()
        newsletter.process_outbox()
        self.assertEqual(len(self.calls), before)
        opted_out = self.publish(notify=False)
        newsletter.queue_first_publication(opted_out['id'], True)
        posts.get_db().commit()
        self.assertEqual(len(self.jobs(opted_out['id'])), 0)

    def test_outage_retry_preserves_payload_and_idempotency_key(self):
        self.join()
        post = self.publish()
        self.failure = True
        self.assertEqual(newsletter.process_outbox()['retry'], 1)
        first = self.calls[-1]
        self.assertEqual(newsletter.notification_status(post['id'])['status'], 'retry')
        stored = self.jobs(post['id'])[0]
        self.assertNotIn('Sensitive', stored['last_error'])
        # Even a later title/from/domain change must not change retry payload.
        self.app.config['EMAIL_FROM'] = 'other@letters.example.com'
        self.app.config['PUBLIC_BASE_URL'] = 'https://changed.example.com'
        posts.get_db().execute('UPDATE posts SET title=? WHERE id=?', ('Changed title', post['id']))
        posts.get_db().commit()
        newsletter.retry_post_notification(post['id'])
        self.failure = False
        self.assertEqual(newsletter.process_outbox()['sent'], 1)
        self.assertEqual(self.calls[-1], first)
        self.assertEqual(newsletter.notification_status(post['id'])['status'], 'sent')
        before = len(self.calls)
        newsletter.process_outbox()
        self.assertEqual(len(self.calls), before)

    def test_old_ambiguous_send_is_held_instead_of_duplicated(self):
        self.join()
        post = self.publish()
        self.failure = True
        newsletter.process_outbox()
        posts.get_db().execute('UPDATE email_jobs SET first_attempt_at=?,available_at=0 WHERE post_id=?', (time.time() - 24 * 3600, post['id']))
        posts.get_db().commit()
        self.failure = False
        before = len(self.calls)
        self.assertEqual(newsletter.process_outbox()['needs_review'], 1)
        self.assertEqual(len(self.calls), before)
        newsletter.retry_post_notification(post['id'])
        self.assertEqual(newsletter.notification_status(post['id'])['status'], 'needs_review')

    def test_atomic_lease_prevents_competing_worker_from_claiming_job(self):
        self.join()
        self.publish()
        claimed = newsletter._claim()
        self.assertIsNotNone(claimed)
        self.assertIsNone(newsletter._claim())
        self.assertEqual(newsletter.process_outbox()['sent'], 0)

    def test_expired_lease_reuses_same_key_and_payload(self):
        self.join()
        post = self.publish()
        claimed = newsletter._claim()
        posts.get_db().execute('UPDATE email_jobs SET lease_until=0 WHERE post_id=?', (post['id'],))
        posts.get_db().commit()
        self.assertEqual(newsletter.process_outbox()['sent'], 1)
        self.assertEqual(self.calls[-1][1], claimed['idempotency_key'])
        self.assertEqual(self.calls[-1][0], json.loads(claimed['payload']))

    def test_archive_and_delete_cancel_pending_notifications(self):
        self.join()
        archived = self.publish()
        columns = {row['name'] for row in posts.get_db().execute('PRAGMA table_info(posts)')}
        if 'archived_at' not in columns:
            posts.get_db().execute('ALTER TABLE posts ADD COLUMN archived_at TEXT')
        posts.get_db().execute('UPDATE posts SET archived_at=? WHERE id=?', ('2026-11-19T11:00:00Z', archived['id']))
        posts.get_db().commit()
        self.assertEqual(newsletter.process_outbox()['skipped'], 1)
        removed = self.publish()
        newsletter.delete_post_notifications(removed['id'])
        posts.get_db().execute('DELETE FROM posts WHERE id=?', (removed['id'],))
        posts.get_db().commit()
        self.assertEqual(self.jobs(removed['id']), [])
        self.assertIsNone(posts.get_db().execute('SELECT * FROM notification_events WHERE post_id=?', (removed['id'],)).fetchone())

    def test_unsubscribe_skips_pending_jobs_and_old_confirm_cannot_reactivate(self):
        confirmation = self.join()
        post = self.publish()
        payload = json.loads(self.jobs(post['id'])[0]['payload'])
        token = re.search(r'/unsubscribe/([A-Za-z0-9_-]+)', payload['text']).group(1)
        self.assertTrue(newsletter.token_info(token, 'unsubscribe'))
        self.assertTrue(newsletter.unsubscribe(token))
        self.assertTrue(newsletter.unsubscribe(token))
        self.assertTrue(newsletter.confirm(confirmation))
        subscriber = posts.get_db().execute('SELECT * FROM subscriptions').fetchone()
        self.assertIsNotNone(subscriber['unsubscribed_at'])
        before = len(self.calls)
        newsletter.process_outbox()
        self.assertEqual(len(self.calls), before)
        self.assertEqual(self.jobs(post['id'])[0]['status'], 'skipped')

    def test_immediate_explicit_resignup_sends_new_confirmation_without_reviving_old_token(self):
        old_confirmation = self.join()
        post = self.publish()
        payload = json.loads(self.jobs(post['id'])[0]['payload'])
        unsubscribe_token = re.search(r'/unsubscribe/([A-Za-z0-9_-]+)', payload['text']).group(1)
        self.assertTrue(newsletter.unsubscribe(unsubscribe_token))
        newsletter.signup('reader@example.com')
        newsletter.signup('reader@example.com')
        count = posts.get_db().execute("SELECT COUNT(*) FROM email_jobs WHERE kind='confirm'").fetchone()[0]
        self.assertEqual(count, 2, 'A fresh explicit signup needs one new confirmation immediately after opt-out.')
        self.assertTrue(newsletter.confirm(old_confirmation))
        self.assertIsNotNone(posts.get_db().execute('SELECT unsubscribed_at FROM subscriptions').fetchone()[0])
        self.assertEqual(newsletter.process_outbox()['sent'], 1)
        new_confirmation = re.search(r'/subscribe/confirm/([A-Za-z0-9_-]+)', self.calls[-1][0]['text']).group(1)
        self.assertNotEqual(new_confirmation, old_confirmation)
        self.assertTrue(newsletter.confirm(new_confirmation))
        self.assertIsNone(posts.get_db().execute('SELECT unsubscribed_at FROM subscriptions').fetchone()[0])

    def test_delivery_stops_when_public_gate_closes_during_a_batch(self):
        self.join()
        self.publish('First notice')
        self.publish('Second notice')
        capture = self.app.config['EMAIL_TRANSPORT']

        def close_gate(payload, key):
            outcome = capture(payload, key)
            self.app.config['PUBLIC_ENABLED'] = False
            return outcome

        self.app.config['EMAIL_TRANSPORT'] = close_gate
        before = len(self.calls)
        result = newsletter.process_outbox()
        self.assertEqual(result['sent'], 1)
        self.assertEqual(result['disabled'], 1)
        self.assertEqual(len(self.calls), before + 1)
        self.assertEqual(posts.get_db().execute("SELECT COUNT(*) FROM email_jobs WHERE kind='post' AND status='queued'").fetchone()[0], 1)

    def test_cancel_after_claim_is_checked_before_provider_request(self):
        self.join()
        post = self.publish()
        original_claim = newsletter._claim

        def cancel_after_claim(config):
            job = original_claim(config)
            if job and job.get('post_id') == post['id']:
                newsletter.cancel_post_notifications(post['id'])
                posts.get_db().commit()
            return job

        before = len(self.calls)
        with patch.object(newsletter, '_claim', side_effect=cancel_after_claim):
            result = newsletter.process_outbox()
        self.assertEqual(result['skipped'], 1)
        self.assertEqual(len(self.calls), before)
        self.assertEqual(self.jobs(post['id'])[0]['status'], 'skipped')

    def test_hold_after_claim_releases_unsent_lease_without_starting_retry_clock(self):
        self.join()
        post = self.publish()
        original_claim = newsletter._claim

        def hold_after_claim(config):
            job = original_claim(config)
            self.app.config['PUBLIC_ENABLED'] = False
            return job

        before = len(self.calls)
        with patch.object(newsletter, '_claim', side_effect=hold_after_claim):
            result = newsletter.process_outbox()
        self.assertEqual(result['disabled'], 1)
        self.assertEqual(len(self.calls), before)
        job = self.jobs(post['id'])[0]
        self.assertEqual(job['status'], 'retry')
        self.assertEqual(job['attempts'], 0)
        self.assertIsNone(job['first_attempt_at'])
        self.assertIsNone(job['lease_token'])

    def test_configuration_failure_does_not_prevent_publish_and_recovers_explicitly(self):
        self.join()
        self.app.config['EMAIL_MODE'] = 'disabled'
        post = self.publish('Original publication title')
        self.assertEqual(newsletter.notification_status(post['id'])['status'], 'configuration_required')
        self.assertIsNotNone(posts.published_by_slug(post['slug']))
        posts.get_db().execute('UPDATE posts SET title=? WHERE id=?', ('Later edit', post['id']))
        posts.get_db().commit()
        self.app.config['EMAIL_MODE'] = 'test'
        newsletter.retry_post_notification(post['id'])
        self.assertEqual(len(self.jobs(post['id'])), 1)
        self.assertIn('Original publication title', json.loads(self.jobs(post['id'])[0]['payload'])['text'])

    def test_archive_restore_cancels_configuration_held_event_before_explicit_retry(self):
        self.join()
        self.app.config['EMAIL_MODE'] = 'disabled'
        post = self.publish('Held notice must never send after restore')
        self.assertEqual(newsletter.notification_status(post['id'])['status'], 'configuration_required')
        edit = posts.get_edit(post['id'])
        archived = posts.archive_post(post['id'], edit['version'])
        restored = posts.restore_post(post['id'], archived['version'])
        self.assertEqual(restored['published_at'], post['published_at'])
        self.app.config['EMAIL_MODE'] = 'test'
        newsletter.retry_post_notification(post['id'])
        self.assertEqual(newsletter.notification_status(post['id'])['status'], 'skipped')
        self.assertEqual(self.jobs(post['id']), [])
        before = len(self.calls)
        self.assertEqual(newsletter.process_outbox()['sent'], 0)
        self.assertEqual(len(self.calls), before)

    def test_archive_restore_never_revives_failed_or_ambiguous_jobs(self):
        self.join()
        for state in ('failed', 'needs_review'):
            with self.subTest(state=state):
                post = self.publish('Previously ' + state)
                posts.get_db().execute('UPDATE email_jobs SET status=? WHERE post_id=?', (state, post['id']))
                posts.get_db().commit()
                edit = posts.get_edit(post['id'])
                archived = posts.archive_post(post['id'], edit['version'])
                posts.restore_post(post['id'], archived['version'])
                newsletter.retry_post_notification(post['id'])
                before = len(self.calls)
                self.assertEqual(newsletter.process_outbox()['sent'], 0)
                self.assertEqual(len(self.calls), before)
                self.assertEqual(self.jobs(post['id'])[0]['status'], 'skipped')

    def test_invalid_canonical_configuration_and_signup_throttle_fail_closed(self):
        for value in ('', None, 'http://public.example.com', 'https://evil@example.com', 'https://blog.example.com/path', 'https://blog.example.com/?x=1', 'https://blog.example.com:bad', '\x00https://blog.example.com'):
            with self.subTest(value=value):
                self.app.config['PUBLIC_BASE_URL'] = value
                with self.assertRaises(newsletter.NewsletterUnavailable):
                    newsletter.signup('reader@example.com')
        self.app.config['PUBLIC_BASE_URL'] = 'https://blog.example.com'
        self.app.config['SIGNUP_LIMIT'] = 1
        newsletter.signup('reader@example.com', 'one-address')
        with self.assertRaises(newsletter.SignupThrottled):
            newsletter.signup('reader@example.com', 'one-address')

    def test_bad_addresses_and_capabilities_rejected(self):
        for address in ('Name <reader@example.com>', 'a\r\nBcc: victim@example.com', 'bad..local@example.com', 'x@-example.com', 'x@nodot', '', None):
            with self.subTest(address=address), self.assertRaises(ValueError):
                newsletter.normalize_email(address)
        self.assertIsNone(newsletter.token_info('x', 'confirm'))
        self.assertFalse(newsletter.confirm('x' * 48))
        self.assertFalse(newsletter.unsubscribe('x' * 48))


if __name__ == '__main__':
    unittest.main()
