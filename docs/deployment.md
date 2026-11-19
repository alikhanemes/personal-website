# Alternative Render and Resend setup

## Current handover state

The active zero-cost hosting setup is documented in [Cloudflare](cloudflare.md).
GitHub authentication was repaired and the blog was pushed to `main`. The Render
instructions below are retained as an alternative requiring a paid persistent
disk; do not use them for the current free deployment. No provider credentials
belong in Git or chat. Resend is connected in test mode; current delivery checks
are recorded in [verification-phase3.md](verification-phase3.md). The owner
confirmed phone sign-in on the hosted Cloudflare preview.

The existing remote is `https://github.com/alikhanemes/personal-website.git`.
Nothing here changes unrelated DNS/mail records or the existing `CNAME` file.

## One service and a persistent disk

`render.yaml` defines one Python web service, one 1 GB disk mounted at
`/var/data/blog`, and a single Gunicorn process with four request threads. The
checked-in Tiptap bundle needs no Node build on Render. `wsgi.py` creates the app,
runs idempotent schema migrations on the mounted disk, and starts the small
in-process outbox/cleanup loop. SQLite leases and provider idempotency keys protect
retries. A separate worker, cron service, database service, or public upload host
is not required.

Build: `pip install -r requirements.txt`.
Start: `gunicorn --config gunicorn.conf.py wsgi:app`.
Health check: `/healthz`. No build/pre-deploy command accesses the disk.
Disk-backed deployments briefly interrupt service; horizontal scaling is disabled.
See [Render’s disk rules](https://render.com/docs/disks).

Validate before provisioning with `render blueprints validate render.yaml` when
the Render CLI is available. The CLI was unavailable for this handover, so Render
CLI/platform validation remains outstanding. Review the paid compute/disk
selection in the Dashboard. Commit the reviewed application and Blueprint, push
them to the intended branch, and then open
[the repository’s Render Blueprint setup](https://dashboard.render.com/blueprint/new?repo=https://github.com/alikhanemes/personal-website).
Render must be able to read that branch. Automatic deploys and custom domains are
deliberately absent from the initial cutover.

## Host configuration

Set these service environment variables. `sync: false` entries prompt during
initial Blueprint creation; subsequent changes belong in the service’s
Environment page. Update the Blueprint’s non-secret values too when deliberately
changing them, so a later sync does not undo a Dashboard change.

| Key | Initial value |
| --- | --- |
| `FLASK_SKIP_DOTENV` | `1` |
| `BLOG_ENV` | `production` |
| `BLOG_DATA_DIR` | `/var/data/blog` |
| `BLOG_PUBLIC_ENABLED` | `false`, until private content review passes |
| `BLOG_EMAIL_MODE` | `test` |
| `BLOG_EMAIL_TEST_ADDRESS` | Your explicitly approved setup/test address; host settings only |
| `BLOG_PUBLIC_BASE_URL` | Actual `https://…onrender.com` preview URL, initially |
| `BLOG_EMAIL_FROM` | Verified sender, e.g. `Alikhan <posts@news.alikhan-zarubay.com>` |
| `RESEND_API_KEY` | Secret from Resend; host settings only |

Production enables Secure cookies and trusts Render’s HTTPS proxy. Do not expose
this production process directly without its proxy. The initial public gate
returns 503 for every route except `/healthz`; this includes `/shygarma`. Use the
running service’s Render Shell for setup, not an ephemeral shell or one-off job.
In that shell:

```sh
python -m flask --app app init-db
python -m flask --app app set-admin
python -m flask --app app review-restored-data
```

Choose and confirm a password of at least 12 characters at the hidden prompt.
Do not put a plaintext password in an environment variable or shell command.
The hash lives on the disk. `set-admin` revokes existing sessions. Do not run seed.

To inspect the private imported inventory from Render Shell before opening the
gate, run `python -m flask --app app shell`, then:

```python
import posts
[(post['id'], post['title'], post['slug'], post['published_at'], post['archived_at']) for post in posts.editor_list_posts()]
exit()
```

This displays private metadata only in the authenticated shell; keep its output
private. The review command checks media file integrity and references.

## Resend and test delivery

Add `news.alikhan-zarubay.com` (or another chosen sending subdomain) in Resend.
Add only the exact verification/DKIM/SPF records Resend supplies for that
subdomain in Namecheap. Preserve existing root MX, SPF, DKIM, DMARC, unrelated
TXT records, nameservers, and website records. Do not enable inbound receiving or
replace existing mail routing. Wait for Resend’s domain verification before
setting the sender/API key. See
[Resend’s domain guidance](https://resend.com/docs/dashboard/domains/introduction).

Keep `BLOG_EMAIL_MODE=test` throughout setup. Only the explicitly approved test
address above can sign up or receive email in this mode. Signup sends a confirmation
link; only confirmed, active SQLite subscriptions join the newsletter audience.
Resend delivers individual short notices containing the article permalink and an
unsubscribe link; unconfirmed addresses do not receive post notices.

The runtime loop processes the durable outbox periodically. For a manual pass:

```sh
python -m flask --app app process-email --limit 20
```

Publish only queues work and remains functional during provider outages. In the
editor, inspect status and retry safely. Each recipient/job has an immutable
payload and stable idempotency key. Ambiguous sends outside the safe retry window
require provider reconciliation (`needs_review`); do not delete/recreate their
records to force a resend. Update, Restore, and a second Publish do not create a
second event. Archive/delete skip unstarted notices. An email already accepted by
the provider cannot be recalled. Switch to `live` only after the full test workflow
and deliberate owner approval; keep the Blueprint consistent.

## Private content import

Start production empty. If importing real local articles, first make a verified
backup using [the backup procedure](deletion-and-backups.md). Never commit SQLite,
uploads, admin hashes, subscriber records, or transfer archives. Inspect a
disposable copy locally and review both published bodies and working copies
before treating a row as a sample. Permanently delete unwanted samples from that
copy only. Do not discard a sample row that now contains real writing: preserve
its working copy and stable URL, and finish its intended public revision.
Export the reviewed copy with `backup-data`; production rejects sample labels
and unchanged known placeholder bodies. A slug by itself never makes real writing
a sample. Preserve real slugs/dates/bodies and working copies. Keep the latest
deletion ledger separately.

Create the final transfer backup in PowerShell after pointing `BLOG_DATA_DIR` at
that reviewed copy, and upload using the SSH destination from Render’s Connect
menu (after adding your public SSH key to Render):

```powershell
& .\venv\Scripts\python.exe -m flask --app app backup-data --output .local-backups/production-import
$renderSsh = Read-Host 'Paste Render SSH destination: srv-…@ssh.REGION.render.com'
scp -r .local-backups/production-import "${renderSsh}:/tmp/"
# If you have a newer external deletion ledger, transfer it as well:
# scp .local-backups/latest-deletion-ledger.json "${renderSsh}:/tmp/latest-deletion-ledger.json"
```

Use a current OpenSSH/SFTP client and verify Render’s SSH host fingerprint. In
the running service’s Render Shell, with public gate still false:

```sh
python -m flask --app app restore-data --source /tmp/production-import
# Add --deletions /tmp/latest-deletion-ledger.json when supplying a newer ledger.
python -m flask --app app cleanup-media
python -m flask --app app review-restored-data
python -m flask --app app review-restored-data --approve
python -m flask --app app set-admin
```

The review checks SQLite integrity, references, indexed image files, and deletion
history. Restored pending email is skipped and all restored sessions are revoked.
No imported admin password is used. Review each expected title/slug/date and image
against the local source before enabling public access. An approved review clears
the restore hold; the independent `BLOG_PUBLIC_ENABLED=false` gate remains closed.

## Preview verification and domain cutover

After private review, set `BLOG_PUBLIC_ENABLED=true` and deploy the existing build.
Keep the custom domain untouched while checking the hosted HTTPS preview:

1. Main/Posts/permalinks contain expected real articles and assets, no samples.
2. Sign in; test Archive, Restore, and typed permanent deletion using disposable
   posts, including one shared image and direct old article/media URLs.
3. Check spoiler click/tap/keyboard and reduced motion at desktop/phone widths.
4. Using only the test address, Subscribe, follow confirmation, publish with
   Notify selected, retry without duplication, and follow unsubscribe.
5. Save a disposable draft and image, redeploy, then reopen both. Test fresh-tab
   verification and sign in on the actual phone over HTTPS.

Only after these pass: record the existing Namecheap website DNS values for
rollback; add `alikhan-zarubay.com` in Render’s Custom Domains; apply the exact
apex/`www` targets Render shows. Adjust conflicting records only for those website
hosts, retaining unrelated DNS/mail records. Verify domain/TLS in Render and test
HTTPS on the phone. [Render’s Namecheap guide](https://render.com/docs/configure-namecheap-dns)
documents apex A and `www` CNAME setup. Change `BLOG_PUBLIC_BASE_URL` to
`https://alikhan-zarubay.com` and deploy. Leave test email mode in place until
email verification and approval are complete. Do not remove the old hosting
configuration before rollback is no longer needed.

## Rollback

Keep a native SQLite/media backup and latest external deletion ledger before
deploying changes. For an application regression, first close the public gate
and disable email delivery, then deploy the last compatible application commit.
A code rollback does not undo database migrations; use only a compatible version.
If data recovery is needed, use the held restore/review procedure, never raw
database file replacement. Preserve newer deletions and skip restored email jobs.
Reopen only after verification. If DNS cutover fails, restore only the recorded
apex/`www` website values in Namecheap; leave Resend/mail records alone.
