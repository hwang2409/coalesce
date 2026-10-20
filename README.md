Screenshots for the viewer PR (viewer, part 1). Not for merging.

Current set: PR #5 head f8f5761. Captured on macOS with headless Chromium (Playwright 1.63.0
from the local npx cache, chromium-headless-shell 1243, WebGL through SwiftShader),
1280 x 800 viewport at device scale 2. Every image comes from one run of
`VIEWER_SCREENSHOTS=<dir> uv run --extra test pytest -q` (the full suite; the browser test
publishes real build123d plates to a `ViewerSceneStore` served by a real `ViewerServer`, and
`tests/viewer_smoke.mjs` takes a screenshot after each step):

- empty, controls-disabled (element screenshot of the camera controls), revision-1,
  revision-2, failure (real OCCT fillet failure), front.
- disconnected: the server is stopped with the page open while the failure is outstanding.
- reconnected: a server again on the same port with the same key.
- new-link: the server restarted on the same port with a NEW key; the page stops retrying.
- no-access: a fresh browser context opens the server without the `#token=` fragment
  (now a step of the smoke driver, no longer a one-off script).

Earlier sets: aa269fc (head aae5cb9), f2efafa (head c032103).
