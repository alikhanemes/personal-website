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

The current configuration opens the verified imported site and disables email.
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
Email uses the existing outbox through a five-minute Cloudflare Cron trigger;
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

The original Namecheap DNS used its free email-forwarding MX/SPF records. Keeping
those records does not keep Namecheap forwarding active after moving to custom
nameservers. The owner confirmed no domain email/forwarding is used, so no active
addresses need migration. Preserve the records nevertheless; any future domain
mail requires a separately configured provider. The pre-cutover DNS snapshot is
private at `.local-backups/dns-before-cloudflare-20261119.json`.

Resend remains disabled until its API key and verified sending subdomain are
configured. The owner explicitly chose to keep delivery disabled at this handover.
Put `RESEND_API_KEY`, `BLOG_EMAIL_FROM` and `BLOG_EMAIL_TEST_ADDRESS`
in the Worker's secret settings; set `BLOG_EMAIL_MODE` to `test` in the config
and redeploy. The test address must be the one explicitly authorized by the owner.
The approved setup recipient is `iamzarubay24@gmail.com`.
Do not use `live` during setup. Confirm, broadcast once, retry without duplicates,
and unsubscribe must be verified with that address before enabling real audiences.
Edits/restores do not create broadcasts; importing published posts does not notify.

Code rollback preserves data: `wrangler.cmd rollback <version-id> --config
cloudflare/wrangler.jsonc`. Retain the same Worker/Object identity and storage
binding. Take a private backup first. Do not roll the database back merely to
roll back code. To leave Cloudflare, download and verify an application backup;
the original Flask app can restore its SQLite database and uploads using the
existing Windows commands. DNS rollback requires the saved original DNS records
and a verified working previous host; reverting DNS alone does not restore code.
