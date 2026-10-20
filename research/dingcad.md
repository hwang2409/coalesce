# dingcad: research note for coalesce

Source: final report of an earlier research agent (session `c38764e234a948fd86873680b479a05a`, agent 1). This note keeps that content. Light fact checks done for this note on 2026-10-20 are marked **[checked]**.

- Repo: https://github.com/yacineMTB/dingcad
- Hosted app: https://dingcad.com (guide: https://dingcad.com/guide/)

## Summary

dingcad is now two separate things:

1. **The GitHub repo** is a small local desktop tool. You write JavaScript, it runs a mesh boolean kernel (Manifold), and it reloads the view when you save.
2. **The hosted app at dingcad.com** is newer and built for AI agents. The agent edits a JSON graph of geometry operations over HTTP and asks for camera images.

For coalesce, the hosted app is the more useful thing to study. The source of the hosted app was not found.

## 1. What dingcad is

- **Purpose.** The repo description is "Live reload cad scripting because >lmao UIs" (https://api.github.com/repos/yacineMTB/dingcad) **[checked]**. The README calls it "a replacement for openscad", says it was "mostly autonomously written by an LLM", and says "There are no docs. Just read the code." (README.md).
- **Stack.** C++17 and CMake, raylib for the window and rendering, quickjs-ng as the embedded JavaScript engine, and Manifold (https://github.com/elalish/manifold) as the kernel. Sources: `.gitmodules`, `CMakeLists.txt`, `README.md`.
- **Kernel.** Mesh only, no B-rep. Every value is a `manifold::Manifold`, a triangle mesh with guaranteed manifold topology (`viewer/js_bindings.cpp`, `JsManifold { shared_ptr<manifold::Manifold> }`). There are no faces, edges, surfaces, fillets or chamfers as objects. Curves are tessellated, for example `cylinder({segments: 64})` in `assemblies/MultiSensorHoist.js`.
- **How a model is defined.** A plain JavaScript ES module that exports `scene` (`scene.js`: `export const scene = buildMultiSensorHoist();`). About 50 global functions wrap Manifold:
  - primitives, booleans and transforms
  - hull, extrude/revolve from 2D loops, slice/project, SDF `levelSet`, `loadMesh` for STL input, smooth/refine
  - queries: `volume`, `surfaceArea`, `boundingBox`, `minGap`, `genus`, `status`

  Sources: `API.md` and the registration list at the end of `viewer/js_bindings.cpp`. Modules import other modules (`assemblies/library/*.js`). There is no DSL, no feature tree and no stored parameters. A model is the result of running the script.
- **Render and preview.** A raylib window does toon (cel) shading, back-face outlines and screen-space edge ink from a normal and depth buffer. It has orbit, pan and WASD camera control, plus a grid and axes. Pressing `p` writes binary STL to `~/Downloads/ding.stl`. STL is the only export. Source: `viewer/main.cpp` (shaders, `WriteMeshAsBinaryStl`, main loop).
- **LLMs in the repo.** Only as the author's coding helper. The repo has no agent API, no MCP, no image capture and no structured feedback.
- **LLMs in the hosted app (dingcad.com).** Agents are the main users.
  - The guide says "an ai agent edits your model through an api, and you watch the design take shape live" (https://dingcad.com/guide/).
  - The protocol is HTTP plus a JS SDK (https://dingcad.com/live-api.txt, https://dingcad.com/live-manifold.txt, https://dingcad.com/live-operations.json).
  - A tweet that a search engine attributes to the author says "I'm launching Dingcad. Just copy the prompt and put it into Codex." It was seen only through a mirror site (site.twstalker.com), not on X.
- **License.** GitHub shows none (`"license": null`) **[checked]**, so legally it is all rights reserved. Some vendored STL files have their own license and attribution files (`assemblies/library/models/corne/LICENSE.txt`, `seedcase/LICENSE.txt`).
- **Activity.** 506 stars, 35 forks, 626 KB, created 2024-07-01, last push 2026-01-20 (commit "slop", `a0663e5`) **[checked: stars, forks, created, pushed_at]**. The visible history starts 2025-10-11 with about 35 commits, all by yacineMTB. The open PRs look unrelated: PR #20 adds Playwright tests for an address-search app. Sources: the repo API and https://github.com/yacineMTB/dingcad/commits/master/.

## 2. Architecture of the repo (observed)

- `run.sh` runs CMake and starts `build/viewer/dingcad_viewer`.
- `viewer/main.cpp`:
  - `FindDefaultScene()` looks for `./scene.js`, then `~/scene.js`. If neither exists, it shows a built-in cube plus sphere.
  - `LoadSceneFromFile()` makes a new QuickJS context for each load and calls `RegisterBindings`. It evaluates the file as a module and reads the exported `scene`. It returns a Manifold handle, or the JS stack trace as a status string.
  - `FilesystemModuleLoader` resolves imports and records each imported file as a dependency.
  - The main loop polls `last_write_time` on every dependency each frame and reloads when one changes, or when you press `r`. `ReplaceScene` converts `GetMeshGL()` into raylib meshes, split into chunks of 65535 vertices, and draws them.
  - `p` exports STL.
  - There is no headless mode. You need a GPU window to see output.
- `viewer/js_bindings.cpp` (52 KB): hand-written QuickJS wrappers. Each call copies or wraps a `shared_ptr<Manifold>` and returns a new immutable value. Errors are JS exceptions with short messages ("translate expects (manifold, [x,y,z])"). `status()` maps Manifold error enums to strings.
- `assemblies/` holds user parts plus `library/` (PCB footprints, an ESP32 board, a radio enclosure, imported STLs) and `library/util/ifItFitsIsits.js`. That file makes a clearance cavity by scaling a part about 3% and subtracting the original. It is a mesh workaround for offsets and tolerances.
- `viewer/model_def.man` is a 3-line C++ snippet. It looks like a leftover.

Data flow: JS file(s) → QuickJS → Manifold calls → one mesh → raylib draw (or STL on a key press).

## 3. Ideas for coalesce

### The hosted dingcad.com protocol (the best ideas to copy)

- **The source of truth is a typed JSON graph, not code.** You send `putNode`, `putPart`, `putGroup` and delete commits with stable string ids (`/^[a-zA-Z][a-zA-Z0-9_-]{0,63}$/`), `expectedRevision` for optimistic concurrency, and a `requestId` UUID so retries are safe. A conflict returns 409, and the agent must reread before it edits again. The guide also says "exactly one operation per request". This matches the proposed typed feature graph. **Copy:** `expectedRevision` plus an idempotency key on every mutating MCP tool.
- **The operation set is a closed, machine-readable list** (https://dingcad.com/live-operations.json), and the prompt says "do not invent op names". Math uses a bounded expression AST (`["+","z",["*",2,["sin",...]]]`) instead of free code: "This is data, never JS code." This is a good guard against agents making up names, and a good sandbox. Coalesce can publish its feature schema the same way and validate against it.
  - **[checked]** `live-operations.json` has `schemaVersion: 1`, 58 operations (3D primitives, booleans, transforms, `hull`, `extrude`, `revolve`, `decompose`, `refine*`, `levelSet`, `warp`, 2D ops such as `offset2d`, `slice`, `project`, `extrudeSection`, `revolveSection`), and top-level keys `expressions`, `limits`, `scope`, `unavailable`.
- **Hard limits and preflight checks are written down and enforced:** triangle budgets, node count and depth, a 45 s job limit, refine preflight `inputTriangles * divisions^2 <= 100000`. Limits help the agent plan, and the errors stay structured (`{revision, code, message, nodeId?, actual?, limit?}`). **Copy:** the error shape with a `nodeId`, so the agent knows which feature failed.
- **Named parts and groups** exist so the user can understand the assembly ("Each relevant physical object should remain its own named part"). These are semantic names at part level only. Inside a part there are no faces or edges to refer to, so dingcad avoids the topological naming problem instead of solving it. A warning sign: `decompose [index]` selects a component by index, and the docs admit "Component indices follow the kernel and may change after geometry edits". This is the index-based reference coalesce wants to avoid.
- **The agent gets visual feedback:** `POST /api/cad/scene/camera` with target, distance, azimuth, elevation and explode returns a JPEG (default 512x512, at most 64–128 KB). This is close to the proposed labeled rendered views. What dingcad lacks: no labels or annotations, the render happens in a user's browser tab, and images from public viewers are "observations, not trusted measurements". Coalesce should render headless on the server, so results are deterministic and do not need a browser open.
- **A "complaint jar" endpoint** (`POST /api/cad/scene/complaints`) lets the agent report tool bugs with revision and request ids. It is cheap and useful for building an agent-first tool. It would also fit coalesce as an MCP tool.
- **Protocol versioning for agents:** an `X-Dingcad-Protocol: 3` header, `PROTOCOL_CHANGED`/410/428 responses that point to new instructions, and an instruction to "re-check the guide every five minutes". The idea is good, but a polling rule in the prompt is fragile. MCP tool schemas carry this by themselves.

### Avoid or treat differently

- **Evaluation is asynchronous and depends on the browser.** Geometry builds in the user's browser, so "a saved revision is not proof it appeared on screen", and errors arrive later through `/viewers`. The two docs also disagree: `live-api.txt` says "do NOT wait for each browser", but `live-manifold.txt` says "every saved edit still requires successful browser evaluation feedback before the next edit". Coalesce plans requirements-as-tests after each edit, so it should evaluate on the server, synchronously, inside the commit call, and return checks with the response.
- **Mesh only, STL only.** No STEP, no exact cylinders or holes, no fillets or chamfers, and no wall thickness from B-rep faces. Tolerance is done by scaling (`ifItFitsIsits.js`). This is the main difference from the proposed build123d/OCCT direction. Manifold's strengths are fast, robust booleans and `minGap`. *Inference (not something dingcad does):* if coalesce needs very robust mesh booleans or fast interference checks on meshes, Manifold is still worth a look as a second engine.
- **The repo's script model** (code is the model, no stable ids, full re-run, only a status string on error) is fine for a human in a loop. It is weak for an agent, because the agent must regenerate code and diff it by eye. The hosted product moved away from this to the graph protocol. That is a useful data point for coalesce's choice of "typed graph as truth, SDK as convenience".
- **The queries are thin.** Volume, area, bbox, genus and `minGap` exist in the JS bindings (`js_bindings.cpp`). The hosted docs say "Measurements ... are not shape-building nodes", and no hosted query endpoint was found. Coalesce's planned structured queries (wall thickness, interference, bbox per named feature) would go well beyond it.

### Differences, in short

| Area | dingcad | Proposed coalesce |
|---|---|---|
| Kernel | mesh (Manifold) | B-rep (OCCT) |
| References | part and node ids; sub-shapes not addressable; index-based `decompose` | semantic selectors down to faces and edges |
| Feedback | async browser render plus an optional JPEG | sync checks plus labeled headless views |
| Agent interface | HTTP and a JS SDK, one op per commit | MCP tools and a Python SDK |
| Export | STL | STEP/STL/3MF |

## 4. Observed vs inferred

- **Observed (read in source or docs):** the repo stack, kernel, script model, viewer behavior as written in `viewer/main.cpp` and `viewer/js_bindings.cpp`; the hosted protocol details as written in `live-api.txt`, `live-manifold.txt` and the guide; repo metadata from the GitHub API.
- **Inferred:** "legally all rights reserved" (from no license file); "looks like a leftover" for `model_def.man`; Manifold as a possible second engine for coalesce; all "copy/avoid" recommendations for coalesce.

## 5. Open questions and things that could not be verified

- The hosted dingcad.com code (server, browser evaluator, SDK at https://dingcad.com/live-sdk.mjs) is not in this repo. No public repo for it was found, and the SDK was not downloaded. Everything about the hosted product comes from its public docs.
- The earlier agent did not open `live-operations.json`. (This note did a light check of it; see section 3.)
- The launch tweet was seen only through a mirror site. Not confirmed on X.
- The repo was created in 2024-07, but the visible history starts in 2025-10. Earlier history may have been rewritten. Not checked further.
- The viewer was not built or run. Claims about its behavior come from reading `viewer/main.cpp` and `viewer/js_bindings.cpp`. `js_bindings.cpp` was read only in part (the helpers, primitives, booleans and the registration list).
- No license means coalesce should not copy code from this repo. Borrowing ideas is fine.
