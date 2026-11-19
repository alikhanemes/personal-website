# Cloudflare hosting and recovery

The blog uses Cloudflare Workers Free with one **SQLite-backed Durable Object**.
It keeps Flask, Jinja, the article renderer, Tiptap documents and stable URLs.
No Render instance, D1 database, R2 subscription or paid Workers upgrade is needed.
The public site is https://alikhan-zarubay.com and the private editor is
https://alikhan-zarubay.com/shygarma. The fallback preview remains
https://alikhan-blog.alikhan-blog-editor.workers.dev. Both hostnames use the same
durable database, but browser sign-in cookies are separate. `www` redirects to
the root domain, preserving article paths and queries. HTTP requests on the
custom domain also redirect to HTTPS. Use the hosted editor for live writing;
the original local database is independent and does not automatically synchronize.

`cloudflare/worker.py` adapts the runtime. `cloudflare/storage.py` durably stores
changed 64 KiB chunks of the existing SQLite file, image files and review/deletion
sidecars in one atomic Cloudflare SQL transaction. A single named Object serializes
requests. Native SQLite still implements application transactions, constraints,
autosave version checks and the email outbox. A failed durable commit fails closed;
the next Object instance recovers the last acknowledged state. Images load lazily.
Changing the Worker name, Object binding/class, or `personal-blog-v1` instance key
would select different storage: preserve all of them across deploys and rollbacks.

## Windows PowerShell setup

Daily local development still uses the Flask commands in the main README.
Cloudflare commands additionally need Node.js and these development tools:

```powershell
Set-Location 'C:\Users\Lenovo\Desktop\personal website'
npm ci
& .\venv\Scripts\python.exe -m pip install uv==0.13.9 workers-py==1.17.9
$blogRoot = (Get-Location).Path
$env:PATH = (Join-Path $blogRoot 'venv\Scripts') + ';' + (Join-Path $blogRoot 'node_modules\.bin') + ';' + $env:PATH
& .\node_modules\.bin\wrangler.cmd login --use-keyring
& .\venv\Scripts\python.exe scripts/build-cloudflare.py
Push-Location cloudflare
& ..\venv\Scripts\pywrangler.exe sync
Pop-Location
& .\node_modules\.bin\wrangler.cmd deploy --config cloudflare/wrangler.jsonc
```

`pywrangler sync` downloads the matching Python/Worker environment and installs
the locked dependencies. Keep `cloudflare/pylock.toml` tracked. Generated
`cloudflare/src/`, `python_modules/`, `.venv-workers/`, `.wrangler/` and secrets
are ignored. The build uses an explicit source/template/code-asset allowlist;
it never copies articles, SQLite files, backups, `.env` or admin credentials.

The current configuration opens the verified imported site and enables live
email signup after successful delivery, double opt-in and unsubscribe checks.
All setup messages went only to the explicitly approved test recipient.
For a new site or migration, first set `BLOG_PUBLIC_ENABLED` to `"false"` and deploy.
Once the import has been checked, approve its review hold, set it to `"true"`
and deploy again. Subsequent deploys retain that explicit setting and the same
durable data. Never seed the hosted database.

## Hosted admin password

After the first deploy:

```powershell
& .\cloudflare\.venv-workers\Scripts\python.exe -X utf8 scripts/cloudflare-manage.py set-admin
```

Choose and confirm a password of at least 12 characters at the hidden prompt.
The command uses the existing Wrangler login and sends only a password hash to
Cloudflare's secret settings. The hash is PBKDF2-HMAC-SHA256 with 1,000,000
iterations, verified by PyCryptodome's compiled implementation. Cloudflare's
built-in PBKDF2 API has a lower iteration limit, so it is deliberately not used.
The local admin password is unchanged. Changing the hosted hash revokes hosted
sessions and tab unlocks on the next request. Bookmark `/shygarma` over HTTPS.

Use the **Cloudflare-created Python environment** for management commands above.
Microsoft Store Python can redirect child-process credential lookups. The helper
resolves Wrangler OAuth separately from secret stdin and passes the token only in
the child environment; no token is printed, put in an argument, or written to disk.
If login expires, run `wrangler.cmd login --use-keyring` again in PowerShell.
An API token and temporary Cloudflare account are not needed for interactive setup.

## Private initial transfer

Create a new snapshot and a production copy. These commands preserve local posts
and exclude only known, explicitly labeled sample posts from the copy:

```powershell
$blogStamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$blogSnapshot = ".local-backups\before-import-$blogStamp"
$blogImport = ".local-backups\cloudflare-import-$blogStamp"
& .\venv\Scripts\python.exe -m flask --app app backup-data --output $blogSnapshot
& .\venv\Scripts\python.exe scripts/prepare-cloudflare-import.py --source $blogSnapshot --output $blogImport
$blogUrl = 'https://alikhan-blog.alikhan-blog-editor.workers.dev'
& .\cloudflare\.venv-workers\Scripts\python.exe -X utf8 scripts/cloudflare-manage.py import --url $blogUrl --source $blogImport
& .\cloudflare\.venv-workers\Scripts\python.exe -X utf8 scripts/cloudflare-manage.py status --url $blogUrl
& .\cloudflare\.venv-workers\Scripts\python.exe -X utf8 scripts/cloudflare-manage.py review --url $blogUrl
# Only after checking the imported titles, content, images and stable URLs:
& .\cloudflare\.venv-workers\Scripts\python.exe -X utf8 scripts/cloudflare-manage.py review --url $blogUrl --approve
```

Initial import refuses a nonempty site. Imported sessions are revoked and unsent
notifications are skipped. Import/restore writes a durable review hold before the
new data can be exposed. All maintenance requests require an HMAC signature,
body checksum, recent timestamp and one-use nonce; they reject browser Origins.
The administration key lives in Windows Credential Manager and the host's secret
settings. It grants full administration: protect the Windows account. No URL
tokens, cookies or browser-storage credentials are used by this CLI channel.

## Storage and free-plan limits

This initial adapter enforces **16 MiB for the SQLite file and 16 MiB total for
uploaded images**, below Cloudflare's platform storage allowance. These are
application safety limits so complete backup/restore can fit the Worker's memory;
they can be expanded with a streaming backup implementation later. Reaching a
limit rejects a write and preserves the previous saved state. Export a backup
before expanding storage. No paid service is enabled automatically.

Individual uploads remain limited to 8 MiB. Hosted images are limited to 4
megapixels; the editor resizes larger supported images before upload when the
browser supports image decoding. The server independently validates and reencodes
every image. Local Flask keeps its original 24-megapixel limit.

The Workers Free and SQLite Durable Object quotas stop operations at their limits.
See [Cloudflare's pricing](https://developers.cloudflare.com/durable-objects/platform/pricing/).
Email uses the existing outbox through a one-minute Cloudflare Cron trigger;
there are no background threads or paid background services.

## Back up and restore

Backups include private drafts and subscriber addresses. Keep them outside Git,
public folders and shared cloud directories. Admin/provider secrets are excluded.

```powershell
$blogUrl = 'https://alikhan-blog.alikhan-blog-editor.workers.dev'
$blogStamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$blogZip = ".local-backups\cloudflare-$blogStamp.zip"
& .\cloudflare\.venv-workers\Scripts\python.exe -X utf8 scripts/cloudflare-manage.py backup --url $blogUrl --output $blogZip
$blogRestore = ".local-backups\restore-$blogStamp"
Expand-Archive -LiteralPath $blogZip -DestinationPath $blogRestore
& .\cloudflare\.venv-workers\Scripts\python.exe -X utf8 scripts/cloudflare-manage.py restore --url $blogUrl --source $blogRestore
& .\cloudflare\.venv-workers\Scripts\python.exe -X utf8 scripts/cloudflare-manage.py review --url $blogUrl
# Review first; approval removes the hold:
& .\cloudflare\.venv-workers\Scripts\python.exe -X utf8 scripts/cloudflare-manage.py review --url $blogUrl --approve
```

Restores merge current deletion tombstones and the private deletion ledger into
the backup. Removed posts cannot silently reappear. Unshared image cleanup is
retryable. A failed/interrupted restore stays closed; retry `restore`, then review.
Do not expose a raw provider rollback: first set `BLOG_PUBLIC_ENABLED=false` and
deploy, preserve the newest backup/deletion ledger outside the rollback target,
then restore through this application procedure and review before reopening.

Local backups have no automatic expiry. Cloudflare documents point-in-time
recovery covering up to 30 days; deleted content can remain in that recovery
history. Deletion removes live article/media access, not existing backups or
third-party copies. See [deletion rules](deletion-and-backups.md) and
[Cloudflare recovery](https://developers.cloudflare.com/durable-objects/api/sqlite-storage-api/).

## Domain, email and rollback

Add `alikhan-zarubay.com` as a Free Cloudflare zone and verify imported DNS against
the registrar before switching nameservers. Preserve all MX/TXT/DKIM/other mail
records. Once the zone is active and the hosted preview is verified, attach the
Worker under **Settings > Domains & Routes > Add > Custom Domain**. Only the
website's old A/CNAME records should be replaced. Keep unrelated records intact.
If attachment fails with Cloudflare error 100117, remove the four old GitHub Pages
root A records and the old `www` CNAME in the DNS dashboard, then redeploy. The
Wrangler login has Workers permissions but cannot delete ordinary DNS records.
The current Wrangler routes attach both the root and `www` hostnames, and
`BLOG_PUBLIC_BASE_URL` is `https://alikhan-zarubay.com`. For a new account/domain,
omit those routes until its zone is active and its preview has been verified.
Do not create a public cache rule for articles, drafts or media.

After cutover, an old recursive DNS cache can still return the previous host even
when authoritative DNS is correct. Compare `Resolve-DnsName alikhan-zarubay.com
-Type A` with the same lookup using `-Server 1.1.1.1`. Clearing Windows' DNS cache
does not clear the router/ISP cache. Wait for its TTL, use another network, or
explicitly choose an alternate resolver for that device. This is a device/network
setting, separate from the site's Cloudflare configuration. The owner's approved
Wi-Fi change and exact rollback commands are private in
`.local-backups/network-dns-recovery-20261119.txt`.

The original Namecheap DNS used its free email-forwarding MX/SPF records. Keeping
those records does not keep Namecheap forwarding active after moving to custom
nameservers. The owner confirmed no domain email/forwarding is used, so no active
addresses need migration. Preserve the records nevertheless; any future domain
mail requires a separately configured provider. The pre-cutover DNS snapshot is
private at `.local-backups/dns-before-cloudflare-20261119.json`.

The owner configured the root domain `alikhan-zarubay.com` in Resend and installed
a sending-only API key in Cloudflare's `RESEND_API_KEY` secret. The sender is
`Alikhan <newsletter@alikhan-zarubay.com>` (`BLOG_EMAIL_FROM`); the approved test
recipient is `iamzarubay24@gmail.com` (`BLOG_EMAIL_TEST_ADDRESS`). These settings
are host secrets, never committed credentials. No Gmail sending account or paid
mailbox is required. This config authorizes sending; incoming replies need separate
mail receiving/forwarding configuration.

Resend's current DNS configuration uses its TXT record at `resend._domainkey`,
plus DNS-only CNAMEs `rsend` → `rsend-apne1.forge.rmta.net` and `send` →
`send.forge.rmta.net`. Copy the actual DKIM value from the owner's Resend account;
do not reuse a key from a different domain/team. Preserve existing root MX/SPF
records. DMARC is recommended; no root DMARC policy was visible during setup.

For a new installation, leave `BLOG_EMAIL_MODE=disabled` until the key and sender
are configured. Then set it to `test` and redeploy. Test mode rejects signup and
delivery for every address except `BLOG_EMAIL_TEST_ADDRESS`. Verify confirmation,
one first-publication notice, retry without duplicates and unsubscribe before
setting it to `live` and redeploying. Public signup then requires double opt-in;
publishing still requires an explicit Notify choice. Edits/restores do not create
broadcasts, and importing published posts does not notify.

The current `/emails` API integration counts each recipient against Resend's Free
allowance: 100 emails/day and 3,000/month, including signup confirmations. More
subscribers require a delivery/quota review, not an automatic paid upgrade.
See [Resend's pricing](https://resend.com/pricing). The confirmed audience remains
in SQLite; the app supplies visible and one-click unsubscribe links.

The one-minute Cron processes queued messages. To process one immediately or
inspect safe configuration metadata through the signed administration channel:

```powershell
$blogUrl = 'https://alikhan-blog.alikhan-blog-editor.workers.dev'
& .\cloudflare\.venv-workers\Scripts\python.exe -X utf8 scripts/cloudflare-manage.py status --url $blogUrl
& .\cloudflare\.venv-workers\Scripts\python.exe -X utf8 scripts/cloudflare-manage.py process-email --url $blogUrl
```

Rerunning processing does not resend completed jobs. Temporary failures retain
their original provider key; ambiguous requests older than 23 hours are held for
provider reconciliation. A restore/public review hold or disabled mode prevents
delivery, including through this command. To stop sending, set
`BLOG_EMAIL_MODE=disabled` in `cloudflare/wrangler.jsonc` and deploy; keep the outbox
for review rather than rebuilding or clearing it.

Code rollback preserves data: `wrangler.cmd rollback <version-id> --config
cloudflare/wrangler.jsonc`. Retain the same Worker/Object identity and storage
binding. Take a private backup first. Do not roll the database back merely to
roll back code. To leave Cloudflare, download and verify an application backup;
the original Flask app can restore its SQLite database and uploads using the
existing Windows commands. DNS rollback requires the saved original DNS records
and a verified working previous host; reverting DNS alone does not restore code.
