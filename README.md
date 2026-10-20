# viewer-ux-round-3 screenshots

Real headless Chromium (Playwright, SwiftShader), device scale 2, viewports 1280x800 and 390x844.

- `viewer/<size>-<step>.png`: the steps of `tests/test_viewer_review_flow.py::test_review_flow_reads_clearly_in_a_real_browser`, and of `test_unrelated_part_in_front_is_not_pushed_behind` (`cover-iso`, `cover-top`: a blue part 0.3 mm above a red plate, 77 parts between them in the list), run with `VIEWER_SCREENSHOTS=<dir>` on branch `viewer-ux-round-3` at 229f37d. Fork steps: `fork-tap` (a real tap on the second row: details shown, both parts still visible), `fork-hide` (the swatch toggle hid that part only), `fork-show`. `ended-copy-refused`: the browser rejects the clipboard. Calls go through the real MCP tool handler (`call_server_tool`) into `DocumentService` with the real kernel and the real viewer HTTP server.
- The round-1 stdio-server shots `review-fork-rev8` and `single-body-rev8` are in commit 7b676e2; the part list has changed since, so `fork-iso` and `thickness` replace them.
- `before/`: the review-3 screenshots of revision 8 (dev b22e2ed) showing the z-fighting.

Note: the first commit on this branch (a9108ea) wrongly holds a copy of the source tree; the second commit replaces it with these files only.
