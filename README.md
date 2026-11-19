# Alikhan’s blog — phase 3

Flask, Jinja, SQLite, plain CSS, and a bundled Tiptap editor. Python 3.11+ is
required. Existing data, permalinks, CNAME, Git history, and the old-site backup
are preserved. Deployment files are prepared; a hosted deployment and real email
delivery must be verified before domain cutover.

## Run locally in Windows PowerShell

```powershell
Set-Location 'C:\Users\Lenovo\Desktop\personal website'
# Create only if venv does not already exist:
if (-not (Test-Path .\venv\Scripts\python.exe)) { python -m venv venv }
& .\venv\Scripts\python.exe -m pip install -r requirements.txt
$env:FLASK_SKIP_DOTENV = '1'
$env:BLOG_DATA_DIR = Join-Path (Get-Location).Path 'data'
$env:BLOG_ENV = 'development'
$env:BLOG_PUBLIC_ENABLED = 'true'
$env:BLOG_EMAIL_MODE = 'disabled'
& .\venv\Scripts\python.exe -m flask --app app init-db
# First setup, or to change/reset your chosen admin password:
& .\venv\Scripts\python.exe -m flask --app app set-admin
& .\venv\Scripts\python.exe -m flask --app wsgi:app run --host 127.0.0.1 --port 5000 --no-reload
```

The password prompt is hidden; choose at least 12 characters and confirm it.
Bookmark [the writing room](http://127.0.0.1:5000/shygarma), then enter that
password. [Main](http://127.0.0.1:5000/) shows the newest public article. Keep the
terminal open; Ctrl+C stops it. If port 5000 is busy, stop the previous server.
Gunicorn is used on Linux/Render; Windows continues to use Flask locally.

BLOG_DATA_DIR defaults to data/ beside app.py. It holds SQLite, uploads,
the local admin hash, and the deletion ledger. Keep it outside static/ and Git.
The legacy Spotify .env is not loaded by these commands. Optional Telegram
override: `$env:BLOG_TELEGRAM_URL = 'https://t.me/your_channel'`; empty disables it.
Run `flask --app app seed` only against disposable local data if sample content is
wanted; seeding is repeatable and never part of deployment.

## Write and manage articles

Autosave preserves drafts and private working copies. Preview uses the shared
safe renderer. Publish is explicit; its optional **Notify email subscribers**
choice is offered only the first time. Update preserves the original URL/date.
Archive removes public access while retaining the article and its working copy.
Restore returns the last published version at its original URL/date without an
email. Permanently delete requires the title-specific dialog and typed DELETE.
See [deletion and backup rules](docs/deletion-and-backups.md).

The toolbar supports rich text, lists, quotes, code, spoilers, undo/redo, and
images with captions/alt text. Paste an image or use Image, then select it to move
or edit it. PNG/JPEG/WebP uploads are validated and re-encoded; maximum 8 MiB and
24 megapixels. Draft/archived images stay private unless another public article
references them.

Sessions expire after 12 hours, are revocable, and use HttpOnly cookies. Private
requests require an unlocked tab; writes require CSRF protection. Reload keeps a
valid unlocked tab open; fresh tabs ask for verification. Passwords/session
credentials never enter browser web storage. set-admin and revoke-sessions
revoke existing sessions. [Tab behavior and verification limits](docs/verification.md)
include browser recovery differences.

## Email, hosting, and recovery

- [Render, Resend, private import, domain cutover, and rollback](docs/deployment.md)
- [Backups, held restores, deletion retention, and search removal](docs/deletion-and-backups.md)
- [Tiptap document format](docs/body-format.md)
- [Phase 3 verification and remaining external checks](docs/verification-phase3.md)

Setup email is restricted to the explicitly approved test address configured in
the host's `BLOG_EMAIL_TEST_ADDRESS` setting. Confirmed subscribers are stored in SQLite; Resend
delivers notices. The durable outbox retries with stable provider keys and stops
ambiguous late retries for review. Provider failure never blocks publication.
The wsgi entry point runs the in-process email/cleanup loop; manual processing:

```powershell
& .\venv\Scripts\python.exe -m flask --app app process-email --limit 20
& .\venv\Scripts\python.exe -m flask --app app cleanup-media
```

## Checks and source

```powershell
& .\venv\Scripts\python.exe -m unittest discover -s tests -v
node .\scripts\test-tab-access.mjs
# Only when rebuilding the editor source (the bundled asset runs without Node):
npm ci --cache .qa/npm-cache
npm run build:editor
```

app.py: configuration/public routes; posts.py: storage; editor_routes.py:
authorized editor API; auth.py: admin/session rules; rendering.py: shared body
renderer; media.py: image privacy/cleanup; newsletter.py: subscriptions/outbox;
operations.py: verified backups/restores; runtime.py: local background loop.

The original source backup remains in
.local-backups/old-site-20261119-phase1/; phase 1’s pre-editor data snapshot is in
.local-backups/phase1-before-phase2-20261119/. The phase 2 source/data backup made
before this work remains at .local-backups/phase2-before-phase3-20261119/. All stay
out of public files/Git.
