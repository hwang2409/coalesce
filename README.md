Screenshots for the viewer PR (viewer, part 1). Not for merging.

Current set (commit after f2efafa), PR #5 head aae5cb9. Captured on macOS with headless
Chromium (Playwright 1.63.0 from the local npx cache, chromium-headless-shell 1243, WebGL
through SwiftShader), 1280 x 800 viewport at device scale 2:

- empty, controls-disabled, revision-1, revision-2, failure, front, disconnected,
  reconnected: `VIEWER_SCREENSHOTS=<dir> uv run --extra test pytest -q tests/test_viewer_browser.py`.
  The test publishes real build123d plates to a `ViewerSceneStore` served by a real
  `ViewerServer`; `tests/viewer_smoke.mjs` takes a screenshot after each step.
  controls-disabled is an element screenshot of the camera controls in the empty state.
  disconnected: the test stops the server with the page open; reconnected: it starts a
  server again on the same port with the same key.
- no-access: a one-off Playwright script that opens a live `ViewerServer` URL without
  its `#token=` fragment.

The previous set (f2efafa) came from the smoke driver against the demo process.
