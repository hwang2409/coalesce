# viewer-ux-round-3 screenshots

Real headless Chromium (Playwright, SwiftShader), device scale 2, viewports 1280x800 and 390x844.

- `viewer/<size>-<step>.png`: the steps of `tests/test_viewer_review_flow.py::test_review_flow_reads_clearly_in_a_real_browser`, run with `VIEWER_SCREENSHOTS=<dir>` on branch `viewer-ux-round-3` (viewer code at 4781542; tests at 045da74). Calls go through the real MCP tool handler (`call_server_tool`) into `DocumentService` with the real kernel and the real viewer HTTP server.
- `viewer/<size>-review-fork-rev8.png`: the review-3 model (holes and portcut both cut into `wall`: two coincident bodies), revision 8, after the fix. Real `coalesce` stdio server.
- `viewer/<size>-single-body-rev8.png`: the same flow with portcut cut into `holes` (one body), revision 8. Real `coalesce` stdio server.
- `before/`: the review-3 screenshots of revision 8 (dev b22e2ed) showing the z-fighting.

Note: the first commit on this branch (a9108ea) wrongly holds a copy of the source tree; the second commit replaces it with these files only.
