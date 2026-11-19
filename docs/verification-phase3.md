# Phase 3 verification — 19 November 2026

The final Python suite passes **90 tests**, including the existing phase 1/2
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

Render and Resend dashboards require sign-in, no API credentials are configured,
and GitHub CLI authentication is invalid. No deployment, billing purchase, DNS
change, Git push, or real email was performed. Therefore hosted preview, Resend
domain/delivery verification, Render Blueprint/platform validation, mounted-disk
redeploy persistence, custom-domain TLS, and actual phone HTTPS sign-in remain
unverified. Actual browser Duplicate Tab/recovery behavior retains the limits
documented in `verification.md`. Follow `deployment.md` before domain cutover.
