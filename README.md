# integration screenshots (mcp-viewer-integration)

Real headless Chromium captures of the viewer started by the real `coalesce` MCP server.
Captured at mcp-viewer-integration c3edf34a4338ef07729bea4cfcee4a887039e762, 2560x1600 (1280x800 at device scale 2).

- `revision-2.png`: after `sketch` + `feature` over stdio MCP (60 x 40 x 6 mm plate).
- `revision-3.png`: after an MCP `feature` update of the extrude distance to 10 mm.
- `failure.png`: after a failed MCP mutation (UNKNOWN_ID); revision 3 stays shown.
- `no-access.png`: the viewer opened without the token.

Method: `INTEGRATION_SCREENSHOTS=<dir> uv run --extra test pytest -q tests/test_mcp_viewer_integration.py -k browser`
starts `coalesce --document <tmp>` over stdio, reads the `coalesce viewer:` line from stderr, and drives
Playwright Chromium (`tests/mcp_viewer_smoke.mjs`, Playwright from ~/.npm/_npx) through the three states.
`no-access.png` comes from `VIEWER_SCREENSHOTS=<dir> uv run --extra test pytest -q tests/test_viewer_browser.py`.
The tests assert zero console errors in every state.
