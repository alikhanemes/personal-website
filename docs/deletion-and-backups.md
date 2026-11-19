# Article deletion, backups, and recovery

## What each action retains

Archive retains the published body, working copy, original slug/date, and media
in SQLite/private storage. It removes the article from Main, Posts, and its public
permalink. Main then selects the next newest public article. An image becomes
private when no public article references it; another public article can keep a
shared image public. Restore publishes the stored published body at the original
URL/date. It never promotes unsent working-copy edits or sends a new-post email.

Permanent deletion requires an unlocked admin session, tab authorization, CSRF,
a current version, and exact server-checked `DELETE` confirmation. It removes the
article, working copy/revision records, and pending notification records. Images
referenced by no remaining article or working copy are queued for removal.
Each successful cleanup removes the file and upload record. Failed/interrupted
unlinks remain in SQLite and can be retried from the editor, runtime loop, or CLI:

```powershell
& .\venv\Scripts\python.exe -m flask --app app cleanup-media
```

The reported removed/pending/failed counts describe actual cleanup, not a promise
that every file was removed. A file already absent is safely treated as cleaned.
Shared images remain. The old article URL returns the ordinary 404, without a
deletion notice. A private deletion ledger retains only slug/deletion timestamp
to prevent an old backup from silently restoring the removed article.

HTML and public/private media responses use `Cache-Control: no-store` and are
checked against current publication state. No service worker or site-controlled
article cache is installed. Previously downloaded copies, browser screenshots,
forwarded emails, third-party archives, search caches, and an email already
accepted by Resend cannot be recalled by deleting a post.

## Backup retention

Backups intentionally retain older copies. Local `.local-backups/` snapshots have
no automatic expiry and remain until you remove them from private storage. Keep
backups encrypted, access restricted, and outside public directories/Git; choose
a retention period and manually remove obsolete copies when no longer needed.
Deleting an article does not rewrite existing backups or force-purge provider
snapshots. Render currently documents daily disk snapshots retained for seven
days; its [disk documentation](https://render.com/docs/disks) is the source for
the current policy. Use the application’s SQLite-native backup for recovery.

## Make a verified backup locally

The command copies SQLite consistently with its indexed images under a writer
lock, creates checksums, and verifies database/image references. The backup
contains private drafts and subscriber addresses. It excludes admin.json,
credentials, and caches. Choose a new path; existing backups are not overwritten.

```powershell
Set-Location 'C:\Users\Lenovo\Desktop\personal website'
$env:FLASK_SKIP_DOTENV = '1'
$env:BLOG_DATA_DIR = Join-Path (Get-Location).Path 'data'
$backupStamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$backupPath = Join-Path (Get-Location).Path ".local-backups/blog-$backupStamp"
& .\venv\Scripts\python.exe -m flask --app app backup-data --output $backupPath
# Keep the newest deletion history outside any rollback target:
if (Test-Path (Join-Path $env:BLOG_DATA_DIR 'deletion-ledger.json')) {
    Copy-Item -LiteralPath (Join-Path $env:BLOG_DATA_DIR 'deletion-ledger.json') -Destination .local-backups/latest-deletion-ledger.json
}
```

Backups must be outside BLOG_DATA_DIR and static/. On Render, create one under
`/tmp`, then download it immediately because `/tmp` does not survive deployment:

```sh
python -m flask --app app backup-data --output /tmp/blog-backup-YYYYMMDD-HHMMSS
```

Use the service’s SSH destination from its Connect menu in local PowerShell:

```powershell
$renderSsh = Read-Host 'Paste Render SSH destination: srv-…@ssh.REGION.render.com'
scp -r "${renderSsh}:/tmp/blog-backup-YYYYMMDD-HHMMSS" .local-backups/
scp "${renderSsh}:/var/data/blog/deletion-ledger.json" .local-backups/latest-deletion-ledger.json
```

Replace the timestamp with the chosen new backup path. Keep another private,
off-device copy and the latest ledger separately. A ledger-write warning after
deletion needs attention: retry a backup/ledger export before relying on recovery.

## Restore held for explicit review

Stop the local server first. On Render, close `BLOG_PUBLIC_ENABLED` and disable
email, deploy those settings, then use the running service’s Shell. Do not use a
one-off/ephemeral shell: the service disk must be mounted. Back up current data
and keep the latest deletion ledger before restoring.

For a local restore, point these commands at the intended target data directory:

```powershell
$env:FLASK_SKIP_DOTENV = '1'
$env:BLOG_DATA_DIR = Join-Path (Get-Location).Path 'data'
$env:BLOG_PUBLIC_ENABLED = 'false'
$env:BLOG_EMAIL_MODE = 'disabled'
& .\venv\Scripts\python.exe -m flask --app app restore-data --source .local-backups/CHOSEN-BACKUP --deletions .local-backups/latest-deletion-ledger.json
& .\venv\Scripts\python.exe -m flask --app app cleanup-media
& .\venv\Scripts\python.exe -m flask --app app review-restored-data
# Only after checking the expected titles/slugs/dates/assets and deletion history:
& .\venv\Scripts\python.exe -m flask --app app review-restored-data --approve
$env:BLOG_PUBLIC_ENABLED = 'true'
& .\venv\Scripts\python.exe -m flask --app app set-admin
& .\venv\Scripts\python.exe -m flask --app wsgi:app run --host 127.0.0.1 --port 5000 --no-reload
```

Omit `--deletions` only when there is genuinely no newer external ledger; current
target/backup deletion records are still merged. The restore creates
`restore-review-required.json` before replacing data. Every route except health
stays closed until explicit approval. Restore verifies checksums/SQLite/media,
reapplies known deletions, revokes sessions, and skips old pending email. The
review verifies removed slugs have not returned and rejects production samples.
Review approval also requires file cleanup to finish. An interrupted restore
leaves the hold in place; investigate and rerun recovery rather than deleting it.

Do not replace blog.sqlite3/uploads or restore a raw host disk snapshot directly
into a public service. If raw recovery is unavoidable, set the host public gate
false *before* recovery, independently of the disk snapshot, and retain the newest
ledger outside that disk. Bring the recovered data into an isolated directory,
make a verified application backup, then use restore-data with that newer ledger
and complete review. Without an external newer ledger, an older snapshot cannot
know about later deletions; manually audit every recovered article before approval.

## Search-result removal

After archive/delete, verify the old permalink and affected unshared media return
404 while signed out. For faster Google removal, verify the domain in Search
Console and submit the old URL through Removals; also request affected image URLs
if necessary. Keep the 404 in place for permanent removal. A temporary removal
request alone is insufficient, and robots.txt should not hide the 404 from the
crawler. Follow [Google’s official removal instructions](https://developers.google.com/search/docs/crawling-indexing/remove-information).
Other search engines and third-party archives require their own removal requests.
