# Phase 3 verification — 19 November 2026

The final Python suite passes **98 tests**, including the existing phase 1/2
coverage. Seven JavaScript tab-policy tests also pass. The bundled Tiptap editor
was rebuilt and JavaScript syntax checks pass.

## Automated coverage

- Archive removes Main/Posts/permalink access and preserves the published body,
  working copy, original date, URL, and images. Restore retains those identities
  and never revives a canceled first-publication notification.
- Deletion requires admin authorization, current-tab unlock, CSRF, current edit
  version, and exact DELETE. It removes post/edit/notification records; shared
  images survive, orphan cleanup is durable, failures are reported and retryable.
- Public media follows published references. Withdrawn article/media URLs return
  normal uncached 404s, including concurrent file disappearance.
- Double opt-in, scanner-safe confirmation GET, CSRF-protected signup/confirm,
  capability-authorized one-click unsubscribe, immediate explicit resubscription,
  confirmed-only audience, and test-recipient restrictions.
- First-publication fencing, immutable payloads/provider keys, outage recovery,
  worker leases, conservative late-retry holds, and cancellation in every unsent
  state. Edits and restores never create a second event.
- Production defaults closed, Secure/HttpOnly cookies behind the HTTPS proxy,
  sample-data guards, and no delivery during setup/restore holds.
- Checksummed native SQLite/media backups, merged deletion history, revoked
  restored sessions, suppressed restored outbox, and approval after held recovery.
- Recreating the app with the same data directory retains drafts and uploaded
  images. This simulates an application restart; it is not a Render redeploy test.

## Browser checks

Used disposable `.qa/phase3-browser-data/` at port 5001 and an injected local mock
email transport. No real email was sent or subscriber added to a remote service.

Created a draft with bold text, spoiler, and a file-picker image. Added alt text
and caption, moved the image, restarted the server, reloaded the unlocked tab,
and resumed the saved content. Private preview used the shared article renderer.
Published explicitly with Notify checked and verified Main and the permalink.

Completed signup, email-link confirmation, one short captured notice, and
unsubscribe through browser controls. The notice contained the public permalink
and unsubscribe link. Restore produced no second notice. Provider outage and
retry-without-duplication were exercised by automated transport tests.

Copied the image between two articles through the editor clipboard. Archived
Main and saw the previous article become Main. Archived both: public pages became
empty and the old article and direct image URL returned 404 to requests without
cookies. Restored the first article at its original URL/date. Deleted the archived
second article with typed DELETE: the shared image stayed public (200). Deleted
the final referencing article: the editor reported one file removed, its file was
absent, and the old permalink and direct image URL returned ordinary 404s.

Desktop and phone layout checks used exact-width 1440/390 CSS-pixel QA frames.
The public desktop text column measured 720 px with 18 px body / 42 px headline;
phone text measured 17.5 px with a 32 px headline. Editor toolbar buttons measured
at least 44 px tall and wrapped within the phone viewport. Documents had no
horizontal overflow. QA frames with vertical scrollbars have a slightly smaller
content viewport than their outer width.

Spoilers measured black `rgb(0, 0, 0)` concealed and light gray `rgb(240, 240, 240)`
revealed, with 0.2-second transitions. Width and height did not change. Tested
click at phone width and keyboard Enter in private preview. The CSS includes a
reduced-motion override; an actual OS reduced-motion setting was not exercised.
Nested inline formatting inherits the concealed/revealed colors.

The in-app browser supplied Origin:null for native form navigation. Progressive
form submission with same-origin fetch worked; CSRF/origin checks remain strict.
Standard forms remain available for ordinary browsers without JavaScript.

Screenshots are private, ignored files under `.qa/`:
`phase3-phone-public.png`, `phase3-phone-editor.png`,
`phase3-deletion-result.png`, and `phase3-local-preview.png`.

## Preservation and remaining external checks

Compared every existing post's id/slug/title/body/publication time and every
working-copy row with the pre-phase-3 snapshot: unchanged. Verified all 15 old-site
backup files against their SHA-256 inventory. CNAME and the existing Git remote
remain unchanged. Credentials and legacy caches were left alone.

The old source remains at `.local-backups/old-site-20261119-phase1/`. The
pre-change data snapshot is `.local-backups/phase2-before-phase3-20261119/`.
A new verified application-data backup is `.local-backups/phase3-ready-20261119/`.

The normal local preview is `http://127.0.0.1:5000/`; the editor is `/shygarma`.
Real email is disabled locally. Temporary QA service/tabs were closed afterward.

## Cloudflare hosting verification

GitHub authentication now works. The hosting target changed to Cloudflare Workers
Free with a SQLite-backed Durable Object; Render is only an alternative reference.
No paid upgrade, R2 subscription or billing purchase was made. Email remains
disabled and no real email has been sent.

A separate disposable hosted Worker exercised sign-in with the same one-million-
iteration password verifier, CSRF, private draft reads, spoiler rendering and PNG
upload. Two complete deployments retained an existing session/tab unlock, saved
draft body and private image. HTTP workflow checks then published the post,
verified Main/Posts/permalink behavior, rejected a stale autosave, explicitly
updated a published post while preserving date/URL, and exercised two posts sharing
one image. Archiving both made the image private; restoring reused its original
publication date without notification. Deleting one reference retained the image;
deleting the final reference removed it and returned ordinary uncached 404s.
All disposable posts were deleted afterward.

Eight storage tests cover recovery from a fresh Object, changed chunk writes,
atomic database/image changes, failed durable saves, transaction rollback,
review/deletion sidecars and application storage limits. Hosted maintenance tests
accepted a valid signed request and rejected replays, stale signatures, modified
bodies, browser Origins and GET requests.

The real article and private working copy were imported privately, downloaded
again, and compared exactly to the source snapshot, including slug and publication
date. SQLite integrity and foreign keys passed. Samples were removed only from
the production copy; the original local database and old-site backup remain.
New private snapshots are under `.local-backups/before-cloudflare-20261119/` and
`.local-backups/cloudflare-import-20261119/`.

The opened production preview passed read-only checks: Main and the permanent
article contain the complete imported body and original timestamp, Posts excludes
Main, samples/missing articles return 404, and signed-out private routes return
401. Public HTML contains no editor bundle or Write post link. The sign-in cookie
is Secure, HttpOnly and SameSite=Strict; article/media errors are uncached.

The owner confirmed successful phone sign-in over HTTPS on the hosted preview.
Custom-domain sign-in has a separate cookie and needs a first sign-in there.
The custom domain is attached to the Worker and its certificate validates.
Public DNS points to Cloudflare; the existing five MX records and SPF TXT are
unchanged. `www` returns a 308 redirect preserving the article path and query.
An old local DNS cache initially returned GitHub Pages; checking the new public
Cloudflare address with the real hostname and ordinary TLS validation succeeds.
The complete production HTTP checks also passed against the custom domain:
full imported article, original permalink/date, sample/missing 404s, private-route
401s, editor password gate, secure cookie flags and no-store response headers.
The owner subsequently configured Resend and authorized test-only delivery to
`iamzarubay24@gmail.com`. A real confirmation arrived in Gmail's Inbox from
`Alikhan <newsletter@alikhan-zarubay.com>`; Gmail reported SPF and domain DKIM pass.
The canonical confirmation link was present. GET did not confirm the address;
CSRF-protected POST did. Repeat signup created no duplicate job, and processing
the completed queue again sent zero messages. Signup for a different address
was rejected without creating a subscription. Signed maintenance forgery/replay
checks still pass. A pre-email snapshot is private at
`.local-backups/before-newsletter-20261119.zip`.

At this checkpoint, the live first-publication notice and unsubscribe check are
pending an owner publish from the password-protected editor. Test mode remains
active until that flow passes.
No new browser visual check was performed for the
Cloudflare adapter; it serves the same article CSS verified above. The new
client-side large-image resizing path has not been exercised on a physical phone.
Actual Duplicate Tab/recovery behavior retains the limits in `verification.md`.
Follow `cloudflare.md` for current deployment and recovery commands.
