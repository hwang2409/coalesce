# viewer strict UX round 2: screenshots

Real headless Chromium captures of the coalesce viewer at PR head `b246c07` (branch `viewer-ux-round-2`).

## Method

- `VIEWER_SCREENSHOTS=<dir> uv run --extra test pytest -q tests/test_viewer_browser.py`
- Playwright 1.63 Chromium (`--use-angle=swiftshader`), deviceScaleFactor 2, a real `ViewerServer` on 127.0.0.1 fed by `ViewerSceneStore.publish` / `publish_failure` between steps.
- `1280x800-*.png` and `390x844-*.png`: `tests/viewer_ux_smoke.mjs` (test `test_viewer_frames_fails_and_ends_honestly`), one capture per step after the canvas reports a settled frame.
- unprefixed files: `tests/viewer_smoke.mjs` at 1280x800 (existing flow: revisions, failure, outage + reconnect, stress, restart with a new key, no access).

## States (prefixed files, both viewports)

| file | state | review finding |
|---|---|---|
| empty | first open, parts section says "No parts yet" | 8 |
| no-solids | revision with only a sketch: explicit notice, camera off | 1 (root cause) |
| plate | first solid after an empty revision, fitted; scale = grid | 1, 2 |
| failure | failed edit: last good model stays, repair text first | 1, 5, 9 |
| failure-details | code and raw message under "Error details" | 5 |
| large | 0.5 mm and 2000 mm parts, fitted; "500 mm, grid 500 mm" | 2 |
| long-name-focus | focused part row shows the full name | 7 |
| orbited / held | human orbit kept on a new revision; "Fit new model" offered | 2 |
| refit | after "Fit new model" | 2 |
| reconnecting | brief outage, bounded retry (1280 only: 390 is the same state) | 3 |
| ended | after 15 s unreachable: terminal "Viewer ended" with new-link help | 3 |

`new-link.png`: restart on the same port with a new key (401 path) -> "Viewer ended" at once.
