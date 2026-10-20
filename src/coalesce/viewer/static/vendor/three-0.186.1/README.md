three.js r186 (npm `three@0.186.1`, MIT, see LICENSE), vendored so the viewer works offline.

- `three.module.js`, `three.core.js`: `build/` from the npm package, unchanged.
- `OrbitControls.js`: `examples/jsm/controls/OrbitControls.js`, with one change: its
  `from 'three'` import points at `./three.module.js`, so the page needs no inline import
  map and can keep a strict `default-src 'self'` content security policy.
