Screenshots for the viewer PR (viewer, part 1). Not for merging.

Captured on macOS with headless Chromium (Playwright 1.63.0, chromium-headless-shell 1243,
WebGL through SwiftShader), 1280 x 800 viewport at device scale 2:

- empty, revision-1, revision-2, failure, front: `tests/viewer_smoke.mjs` driving the real
  `python -m coalesce.viewer.demo` process (its printed URL), screenshot after each step.
- no-access, disconnected: a one-off Playwright script against a live `ViewerServer`;
  no-access opens the URL without its `#token=` fragment, disconnected stops the server
  while the page is open.
