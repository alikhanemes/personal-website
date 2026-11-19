# Phase 2 verification — 19 November 2026

The existing 15 tests passed before changes. The final suite has 36 passing
Python tests and 7 passing JavaScript tab-policy tests. They cover schema
preservation, seeding, publication ordering, staged edits, optimistic conflicts,
explicit Publish/Update, stable dates/slugs, safe formatting, media validation and
publication-based visibility, CSRF, throttling, session rotation/expiry/revocation,
and authorization on private routes and assets.

## Browser run

Used a separate local SQLite copy under `.qa/browser-phase2-data/` on port 5001.
Its disposable test admin and sample publications did not change real articles
or configure the real admin account. The normal site remains on port 5000.

Verified through the browser UI:

- Sign in; create and autosave a draft with bold text and a spoiler.
- Insert a PNG through the file picker; add alt text/caption; move it between paragraphs.
- Reload the unlocked tab and resume the saved article and image.
- Preview with public typography, reveal the spoiler, and display the private image.
- Confirm Publish; see the new article on Main and at its permanent URL, with the
  previous Main article now in Posts.
- Autosave a published title edit while the public title remains unchanged;
  confirm Update and see the new title at the original URL and publication date.
- Independently open another tab while signed in: it requests password verification.
- Sign out and confirm the writing room returns to verification.

Separate requests without cookies returned 404 for an unpublished article and
its public image URL, and 401 for its private draft/media endpoints.

Responsive checks used same-origin QA frames with exact 1440 px and 390 px layout
viewports because the browser's viewport override did not apply consistently.
The public desktop body is 720 px wide, 18 px text / 28.8 px line height, with a
42 px headline. The phone body is 17.5 px / 28 px, with a 32 px headline. Public
and editor document widths matched their viewports without horizontal overflow;
toolbar buttons wrap, and figures/images fit. Screenshots are under ignored `.qa/`.

## Limits and tab behavior

Actual browser Duplicate Tab, closed-tab recovery, browser restart restoration,
Safari/iOS/Android, and binary clipboard paste could not be exercised with the
available browser controls. File-picker upload was exercised. JavaScript policy
tests simulate cloned markers, reload, `back_forward`, unavailable/held Web Locks,
continuation-ticket consumption, and bfcache restoration; these are not substitutes
for browser recovery tests on the intended devices.

The tab policy resumes only ordinary `reload` (or a single-use same-tab ticket),
and otherwise requests verification. Browsers can classify duplication/recovery
differently; a recovered document reported as `reload` may resume while its
server session remains valid. Live duplicate ownership is additionally guarded
by Web Locks. Browsers without Web Locks require verification after reload.

Navigation classification references:
[PerformanceNavigationTiming.type](https://developer.mozilla.org/en-US/docs/Web/API/PerformanceNavigationTiming/type),
[notRestoredReasons](https://developer.mozilla.org/en-US/docs/Web/API/PerformanceNavigationTiming/notRestoredReasons),
and [Window.name](https://developer.mozilla.org/en-US/docs/Web/API/Window/name).
