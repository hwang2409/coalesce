# Prior art: tools that let AI agents or code operate CAD

Status: complete. Written 2026-10-20. Metadata (stars, last push, license) from the GitHub REST API on that date unless noted.

## 1. Zoo (formerly KittyCAD)

**What it is.** A CAD company with its own geometry engine, a code-CAD language (KCL), a desktop app (Zoo Design Studio), an AI agent (Zookeeper, the successor to Text-to-CAD), and an MCP server.

- **Kernel.** Own B-rep engine, built from scratch, GPU-oriented, run as a cloud service ("CAD-as-a-Service"; "By designing Zoo from the ground up to always use an API, we ensure that third-party developers always have access to the exact same functionality that our employees do"). It keeps a minimal set of primitives: fillets and similar features are "B-splines with specific constraints". It holds a US patent for GPU surface-surface intersection. Source: https://zoo.dev/research/zoo-cad-engine-overview. The engine source is not public (not found on https://github.com/KittyCAD; the app repo is MIT, the engine is a remote service). *Inference:* you cannot self-host the kernel; every geometry operation needs Zoo's servers and an API token (https://github.com/KittyCAD/mcp README: "An API key for Zoo ... `ZOO_API_TOKEN`").
- **Model representation.** KCL, a text language for parametric CAD (https://zoo.dev/docs/kcl-lang, https://zoo.dev/research/introducing-kcl). The app lets users "seamlessly switch between point-and-click modeling and defining and editing geometry and annotations as code" (https://zoo.dev/research/zookeeper). KCL is at version 3.0 (https://zoo.dev/docs/kcl-lang/migrating-to-kcl-3). It has units in numeric types, sketch blocks with constraints, and Zoo also open-sourced a constraint solver, `ezpz` (https://github.com/KittyCAD/ezpz).
- **References to faces and edges.** By *tags*: `$myTag` names a face or edge at creation; the docs prefer dot paths such as `extrude001.faces.capEnd001` or `region001.tags.line4`. The docs warn that legacy tags "may leak outside of function scopes and be updated (i.e. re-bound to the face of a different body)", and that `START`/`END` are "generally discouraged since they're ambiguous". Imported STEP geometry is opaque: "there is no access to their internal components" (https://zoo.dev/docs/kcl-lang/types#tags). There is also an `entity_get_index` tool in the MCP server (see below), so index-based access still exists at the engine level.
- **Text-to-CAD and Zookeeper.** Text-to-CAD shipped December 2023 with two endpoints (generate, edit). It is "actually an agentic AI program" integrated with the engine "to generate, modify and validate". Zookeeper shipped January 2026 in Design Studio v1.1. It writes and runs KCL, reads docs, and checks geometry "via multi-view snapshots and computational tools for engineering, such as center of mass location, mass calculation, and surface area and volume measurements". Zoo says they first tried direct text-to-B-rep generation and changed to LLM-writes-KCL. Roadmap: image input, reverse engineering STEP/STL into KCL, fleets of agents. Source: https://zoo.dev/research/zookeeper.
- **MCP server.** https://github.com/KittyCAD/mcp (MIT, 10 stars, pushed 2026-10-19). Python, `uvx zoo-mcp`. Tools seen in `src/zoo_mcp/server.py`: `execute_kcl`, `exec_kcl_project`, `mock_execute_kcl`, `lint_and_fix_kcl`, `format_kcl`, `export_kcl`, `convert_cad_file`, mass/volume/surface area/center of mass/bounding box (for KCL and for CAD files), modeling sessions (`start_modeling_session`, `import_cad_file`), `get_face_info`, `entity_distance`, `select_entities`, `highlight_set_entities`, `edge_get_length`, `curve_get_type`, `entity_get_index`, `get_sketch_constraint_status`, `visualize_sketch`, `snapshot`, and KCL docs/samples search. `snapshot` renders orthographic named views ("so measurements read off the image are not distorted by perspective"), multiple views per call, optional edge highlighting (https://raw.githubusercontent.com/KittyCAD/mcp/main/src/zoo_mcp/server.py).
- **Agent feedback.** KCL errors and lints, multi-view orthographic snapshots, mass properties, entity queries (face info, distance, edge length), sketch constraint status.
- **License.** Design Studio app: MIT (https://github.com/KittyCAD/modeling-app, 1319 stars). MCP: MIT. Engine: closed, hosted. Text-to-CAD UI repo archived (https://github.com/KittyCAD/text-to-cad-ui).
- **Maturity/activity.** Very active (many repos pushed in October 2026, https://github.com/KittyCAD). Funded company with its own kernel, language, app and agent.

**Lessons for coalesce.**
1. Zoo is the closest full-stack competitor. Its feedback tool set (orthographic multi-view snapshots, mass properties, face/edge queries, lint-and-fix) is a good baseline checklist for coalesce's `query`/`render` tools. Coalesce must beat it on something else: local/self-hosted open kernel, typed graph instead of code text, and requirements-as-tests (not found in Zoo docs).
2. Zoo moved from "AI generates B-rep" to "LLM writes code in a CAD language". Its tag system shows that semantic naming of faces at creation time works, but its own docs list ways it leaks. Coalesce selectors should be scoped to a feature and never re-bind silently.

## 2. CadQuery

**What it is.** "A python parametric CAD scripting framework based on OCCT" (https://github.com/CadQuery/cadquery). Kernel: OpenCascade (OCCT) B-rep, through the OCP bindings.

- **Representation.** Python code. Main API is the fluent `Workplane` chain with a hidden stack (`cq.Workplane("XY").box(2,2,2).faces(">Z").shell(-0.2)`). There is also a "free function API" that "has no hidden state" (https://raw.githubusercontent.com/CadQuery/cadquery/master/doc/free-func.rst). A model is the result of running the script; there is no stored feature tree.
- **Selectors.** String selectors such as `">Z"`, `"|Z and >Y"`, `"not(<X or >X)"`, which are shortcuts for selector classes; users can plug in their own (https://raw.githubusercontent.com/CadQuery/cadquery/master/doc/selectors.rst). They select by direction and type, not by name. *Inference:* they are semantic only in a geometric sense ("topmost face"); they can pick a different face after an edit changes geometry.
- **Agent feedback.** Python exceptions and OCCT errors. No built-in agent loop. GUI viewer: CQ-editor (https://github.com/CadQuery/cq-editor, Apache-2.0, 1261 stars).
- **LLM usage.** CadQuery is the main target language in academic text/point-cloud-to-CAD research: CADPrompt benchmark, ICLR 2025, 200 objects (https://proceedings.iclr.cc/paper_files/paper/2025/file/81a934cd364e18ea6fdeaf57a93c17d4-Paper-Conference.pdf); Text-to-CadQuery, 170k CadQuery annotations on Text2CAD (https://arxiv.org/abs/2505.06507); CAD-Recode, point cloud to CadQuery, ICCV 2025 (https://github.com/filaPro/cad-recode); CAD-Coder, SFT + GRPO with geometric reward, NeurIPS 2025 (https://papers.neurips.cc/paper_files/paper/2025/hash/564a224e88f2490f3c1deaae877d37e3-Abstract-Conference.html). These papers come from the search results above; their numbers were not checked against the PDFs.
- **License.** Apache-2.0 (PyPI metadata: https://pypi.org/pypi/cadquery/json; GitHub shows "NOASSERTION").
- **Maturity/activity.** 5961 stars, pushed 2026-10-18, PyPI version 2.8.0 (https://github.com/CadQuery/cadquery, https://pypi.org/project/cadquery/). Mature and active.

**Lessons for coalesce.**
1. LLMs already know CadQuery well because of the training data and the research datasets. *Inference:* a Python SDK that reads like CadQuery/build123d will get more correct first drafts from agents than a new DSL.
2. Direction-based string selectors are compact and easy for an LLM to write, but they are not stable under edits. Coalesce can accept them as one input form, but should resolve them once and store a stable reference.

## 3. build123d

**What it is.** "A python CAD programming library" (https://github.com/gumyr/build123d), also on OCCT via OCP. It grew from CadQuery ideas and is the proposed kernel layer for coalesce.

- **API style: builder mode vs algebra mode.**
  - Builder mode: `with BuildPart() as part:` context managers; objects created in the block are combined into the builder; selectors are methods on the builder (`part.edges(Select.NEW)`) (https://build123d.readthedocs.io/en/latest/key_concepts_builder.html, https://build123d.readthedocs.io/en/latest/_sources/topology_selection.rst.txt).
  - Algebra mode: plain objects and operators: `Box(1,2,3) + Cylinder(0.2,5)`, `-` for cut, `&` for intersect, plus "placement arithmetic" (https://build123d.readthedocs.io/en/latest/key_concepts_algebra.html). *Inference:* algebra mode is the better fit for a feature graph, because each operation is a pure function of its inputs with no hidden context.
- **Selectors.** `vertices()/edges()/wires()/faces()/solids()` return a `ShapeList` with `sort_by`, `group_by`, `filter_by` (by `Axis`, `Plane`, `GeomType`, `Convexity`), `filter_by_position`, operator shortcuts (`part.faces() > Axis.Z`, `part.faces() | Axis.Z`), lambdas, and plain list indexing (`part.faces()[-2:]`) (https://build123d.readthedocs.io/en/latest/key_concepts.html#selectors).
- **History and provenance (important for coalesce).** The current "latest" docs (version shown as 0.13.1.dev) say that every operation (boolean, fillet, chamfer, 2D fillet, join) "keeps a record of what it did to the sub-shapes of its inputs: which it left alone, which it rebuilt, which it created and which it removed". `Select.LAST` gives what the last operation brought in or created; `Select.NEW` gives only features that "existed in no input in any form". Selected shapes also carry a `topo_path` (the route by which they were selected), described as "metadata rather than geometry" (https://build123d.readthedocs.io/en/latest/_sources/topology_selection.rst.txt). Not checked: whether this is in the released PyPI version 0.13.0 (https://pypi.org/pypi/build123d/json) or only on the dev branch. *Inference:* this OCCT history record is the raw material coalesce needs for stable semantic selectors ("the faces created by feature `boss_1`"); coalesce can build on it instead of writing its own naming system from zero.
- **Export.** STEP, STL, 3MF, glTF, DXF, SVG (https://build123d.readthedocs.io/en/latest/_sources/import_export.rst.txt).
- **Agent feedback.** Python exceptions; viewer is a separate project (ocp_vscode). No agent API in the core.
- **License.** Apache-2.0. **Activity:** 3467 stars, pushed 2026-10-19 (https://github.com/gumyr/build123d). Active.

**Lessons for coalesce.**
1. Use algebra mode (pure functions) inside the feature graph executor, and use build123d's operation history (`Select.NEW/LAST`, `topo_path`) to resolve and re-resolve semantic selectors. Pin the build123d version, since this part of the API is new.
2. build123d already has several third-party MCP servers (section 6). The kernel wrapper is not a moat; the moat must be the graph, selectors, checks and feedback.

## 4. OpenSCAD and its LLM usage

**What it is.** "The Programmers Solid 3D CAD Modeller" (https://github.com/openscad/openscad). A declarative CSG language (`.scad`). 10479 stars, pushed 2026-10-18. The last tagged GitHub release is `openscad-2021.01` (https://api.github.com/repos/openscad/openscad/releases/latest); newer work ships as development snapshots (*inference* from the release API; snapshot page not checked).

- **Kernel.** Mesh/CSG, not B-rep. CGAL and, newer, a Manifold backend (`--backend=manifold` in the README's WASM example, https://raw.githubusercontent.com/openscad/openscad/master/README.md). Curves are tessellated (`$fn`). No STEP export from the core (not found in the README).
- **Representation.** Code in a small functional language. No faces or edges to select; there is no topological naming problem because there are no references to sub-shapes.
- **Agent feedback.** Compiler errors/warnings, rendered PNG via CLI, STL output. Nothing structured.
- **License.** GPL with a special exception for linking CGAL (https://raw.githubusercontent.com/openscad/openscad/master/COPYING).
- **LLM usage.**
  - CADAM by Adam (https://github.com/Adam-CAD/CADAM, GPL-3.0, 5297 stars, pushed 2026-10-15): "The Open Source Text to CAD Web App". Natural language and images to OpenSCAD code, OpenSCAD compiled to WASM, Three.js preview, "Parameter Extraction" into sliders so "parameter changes [happen] without AI re-generation", export STL/SCAD/DXF. Hosted at https://adam.new/cadam.
  - MCP servers: https://github.com/jhacksman/OpenSCAD-MCP-Server (208 stars), https://github.com/RobertCoop/openscad-mcp (159 stars) (GitHub search, see section 6).
  - Community pushback exists: an r/openscad thread titled "Can we have an OpenSCAD LLM ban?" (https://www.reddit.com/r/openscad/comments/1shai33/can_we_have_an_openscad_llm_ban/) (title seen in search results only; not read).

**Lessons for coalesce.**
1. OpenSCAD is the default "LLM writes CAD" target because it is small and has a lot of public code. Its limit is the same as dingcad's: mesh only, no exact faces, no STEP. That is where a B-rep tool wins for mechanical parts.
2. CADAM's "extract parameters, then change them without calling the model again" is a good pattern: coalesce's typed graph gives this for free; expose parameters as first-class, cheap edits.

## 5. Onshape API and FeatureScript as an agent surface

**What it is.** Commercial cloud CAD by PTC. Kernel: Parasolid B-rep (not checked in this session; widely known, *treat as unverified*). Models are a feature list; FeatureScript is the language that the built-in features themselves are written in, and users write custom features in a "Feature Studio" (https://cad.onshape.com/FsDoc/library.html).

- **Queries (the most relevant idea).** FeatureScript selects entities with `Query` values. The docs say: "There are two general types of queries: state-based and historical. State-based queries select entities based on the model state, e.g. 'All edges adjacent to a cylindrical face which touches this point.' Historical queries select entities based on the model history, e.g. 'the edge that was generated by feature extrude_1_id from sketch vertex vertex_id ...'. State-based queries cannot refer to entities that have been deleted. Most automatically-generated queries are historical." Examples: `qCreatedBy(id + "startingCube", EntityType.EDGE)`, `qContainsPoint(...)`. Index access exists but is discouraged by its own description: `qNthElement` — "The order of entities resolved by a query is deterministic but arbitrary." (https://cad.onshape.com/FsDoc/library.html).
- **Official MCP.** "Onshape FeatureScript MCP Server", from Onshape Labs (early access), subscribed through the App Store; blog dated 09/03/2026. The assistant "can write FeatureScript directly into a document, review errors, iterate". Scope is FeatureScript custom features, not the full REST API (https://www.onshape.com/en/blog/get-started-featurescript-mcp-server; PTC press release: https://investor.ptc.com/resources/news/news-details/2026/PTC-Advances-Leadership-in-AI-Driven-Design-with-Launch-of-Onshape-FeatureScript-MCP-Server/default.aspx, not opened). Onshape also has "AI Advisor" (https://www.onshape.com/en/features/ai-advisor, not opened).
- **Community MCP servers** over the REST API: https://github.com/hedless/onshape-mcp (154 stars, no license), https://github.com/ReshefElisha/jarvis-onshape-mcp (174 stars, "Vision-decomposition"), https://github.com/gpambrozio/onshape-mcp ("87 tools for modelling, assemblies and export"), https://github.com/altendky/onshape-mcp (Apache-2.0). Not read in depth.
- **Agent feedback.** FeatureScript errors, feature regeneration status in the Part Studio; REST API gives mass properties, exports, shaded views (not checked in this session).
- **License.** Proprietary SaaS; API rate allocations apply (the blog recommends the "company API allocation" for Claude Code, per the search summary; not read directly).

**Lessons for coalesce.**
1. FeatureScript's historical queries (`qCreatedBy(featureId, EntityType.FACE)`) plus state-based filters are the proven, production answer to "semantic selectors". Coalesce selectors should copy this two-layer model: history ("created by feature X") first, geometric filters ("the planar one facing +Z") second, and index only as a last resort that the checker flags.
2. Even PTC chose "agent writes code in the CAD's own language, then reads errors" as the first official agent surface. Requirements checks and structured geometry queries are not part of that MCP (not found in the blog).

## 6. Existing CAD MCP servers (GitHub search)

Searched on 2026-10-20 with the GitHub search API (`q=<term>&sort=stars`). Total repo hits: "cad mcp" 833, "freecad mcp" 186, "blender mcp" 1141, "onshape mcp" 34, "build123d mcp" 56, "cadquery mcp" 48, "openscad mcp" 64, "fusion 360 mcp" 93, "solidworks mcp" 119, "kicad mcp" 232. Star counts below are from that search.

### Most relevant to coalesce (code-CAD on OCCT, agent-first)

- **build123d-mcp** — https://github.com/pzfreo/build123d-mcp (Apache-2.0, 121 stars, created 2026-04-30, pushed 2026-10-15). "Give your AI CAD eyes." Persistent build123d session; render PNG/SVG/DXF; measure volume/area/bbox/topology/center of mass; "Find holes, bosses, countersinks, and hole patterns"; printability checks; session snapshots; export STEP/STL/DXF/SVG; `validate()`. Feature recognition on imported STEP: `find_candidates(kind="hole", ...)`, then `edit_feature(handle, diameter=8, protected_refs='["@feature[...]"]')`, which "reports predicted and measured added or removed volume" and "registers the result only when the hole, other recognised holes, and the outer envelope pass its checks". Claim: on CADGenBench (June 2026) it raised one model's score "from 0.360 to 0.457 and CAD validity from 88% to 100%" (README, https://raw.githubusercontent.com/pzfreo/build123d-mcp/HEAD/README.md). Benchmark claim not verified.
- **agentcad** — https://github.com/jdilla1277/agentcad (Apache-2.0, 168 stars, created 2026-06-02, pushed 2026-10-19), site https://agentcad.dev. "CAD tool for AI agents." CLI + MCP; agent writes build123d scripts (CadQuery as compatibility mode); every command returns JSON on stdout. Required workflow "Inspect → Specify → Build → Validate → Deliver". Has `inspect` (topology, "addressable IDs, edit risk"), `measure`, multi-view `render`, a `spec.json` of requirements checked by `agentcad check-spec`, versioned runs, `diff` between versions (shared/only-in-one volume), a live browser viewer, and spatial review comments pinned to a surface. It claims a "48% higher CAD Score" on CADGenBench with GPT-5 Nano (README, https://raw.githubusercontent.com/jdilla1277/agentcad/HEAD/README.md). Benchmark claim not verified. **This is the closest open-source project to coalesce found** (see Threats).
- Others (not read in depth): https://github.com/Svetlana-DAO-LLC/cad-agent (build123d + MCP, 51 stars), https://github.com/armpro24-blip/cad-cae-copilot (76 stars), https://github.com/rishigundakaram/cadquery-mcp-server ("CAD generation and verification tools", 20 stars), https://github.com/jzjzzzzzzz/IntentForge ("Intent-preserving deterministic CAD pipeline for editable, validated CadQuery models", 5 stars), https://github.com/Alfredoalv13/shapr3d-mcp (build123d + STEP, 39 stars).

### Bridges into existing desktop CAD

- **FreeCAD:** https://github.com/neka-nat/freecad-mcp (MIT, 2959 stars, pushed 2026-10-13). Tools: `create_object`, `edit_object`, `delete_object`, `get_objects`, `get_view` (screenshot), `execute_code`, `execute_code_headless`, `insert_part_from_library`, `run_fem_analysis` (CalculiX); most tools return a screenshot by default (`include_screenshot`, `view_name`) (https://raw.githubusercontent.com/neka-nat/freecad-mcp/HEAD/docs/tools.md). Also https://github.com/ghbalf/freecad-ai (LGPL-2.1, 578 stars, an in-app assistant workbench), https://github.com/spkane/freecad-addon-robust-mcp-server (257 stars).
- **Blender:** https://github.com/ahujasid/mcp-for-blender (MIT, 31708 stars, the most-starred item in this survey). Nine tools; the main one is `execute_blender_code` ("The AI writes Blender Python itself"); `look` returns viewport/camera/multi-angle images in solid/wireframe/x-ray modes; `get_scene_info`; `generate_3d` via Tripo/Hunyuan3D/Rodin/Meshy; optional `BLENDER_MCP_SAFE_MODE` to block risky code (https://raw.githubusercontent.com/ahujasid/mcp-for-blender/HEAD/README.md). Mesh, not CAD.
- **Fusion 360 / SolidWorks / others:** many small servers, e.g. https://github.com/faust-machines/fusion360-mcp-server (127 stars), https://github.com/AuraFriday/Fusion-360-MCP-Server (127), https://github.com/eyfel/mcp-server-solidworks (AGPL-3.0, 405), https://github.com/wzyn20051216/solidworks-automation-skill (1303), https://github.com/Cai-aa/CAD-Agent-Hub (CATIA/SolidWorks/NX/Fusion, 102). https://github.com/daobataotie/CAD-MCP (620 stars; AutoCAD-style 2D, from description). Not read in depth.
- **Zoo** official MCP: see section 1. **Onshape**: see section 5. **OpenSCAD**: see section 4. **KiCad** (PCB, adjacent): https://github.com/mixelpixx/KiCAD-MCP-Server (2866 stars).

**Pattern across bridges (inference from the tool lists above).** Almost all of them expose "run arbitrary code" plus "take a screenshot" plus "list objects". Few expose structured geometry queries, and none of those read expose requirement checks except agentcad (`check-spec`) and build123d-mcp (`validate`, protected refs).

**Lessons for coalesce.**
1. "Execute code + screenshot" is the commodity baseline and is not enough to win. The differentiators that exist (agentcad's spec checks and diffs, build123d-mcp's feature handles and protected refs) are exactly coalesce's planned direction, so coalesce must do them deeper (typed graph, not post-hoc recognition on a STEP).
2. CADGenBench (https://huggingface.co/spaces/HuggingAI4Engineering/CADGenBench) is being used as the public scoreboard by both closest competitors. *Inference:* coalesce should measure itself on it early.

## 7. SolidCraft (new open-source parametric CAD with built-in MCP)

**What it is.** "An open-source parametric 3D CAD application built in pure Rust" with "a parametric feature timeline" and a "built-in MCP server" (https://github.com/tangency-labs/solidcraft). Repo created 2026-10-07 under `tangency-labs` (older links point to `bherbruck/solidcraft`, so it likely moved; *inference*), 7402 stars on 2026-10-20, pushed the same day.

- **Kernel.** B-rep through the Rust `truck` kernel: the architecture doc lists "the B-rep kernel boundary (truck)" (https://raw.githubusercontent.com/tangency-labs/solidcraft/HEAD/docs/architecture.md). The README also links a "SolidCore" project (https://github.com/bherbruck/solidcore); the GitHub API returned nothing for it (not found / private).
- **Representation.** A parametric timeline document (`.solidcraft`). "A script is a JSON list of the same commands the UI runs" (README). Features: sketches with own constraint solver, extrude/revolve/sweep/loft/fillet/chamfer/shell/draft/holes/threads/patterns, parameters with unit-aware expressions, assemblies with joints and interference, sheet metal. Files: STEP, IGES, 3MF, STL, OBJ, glTF (README).
- **Agent surface.** `solidcraft-cli mcp`, headless (GPU screenshots) or bridged to the running app "so you can watch". Tools: `list_commands`, `inspect_design` (parameters, timeline with per-feature error/warning/timing, bodies with volume/area/COM/bbox/topology counts, sketch DOF and solve status), `measure`, `body_topology`, `set_parameter`, `undo/redo`, `execute`, `batch`, `open/save/export`, `screenshot`, async render jobs; MCP tool annotations (read-only vs destructive); path sandboxing (https://raw.githubusercontent.com/tangency-labs/solidcraft/HEAD/docs/mcp.md).
- **References.** Weak point: `body_topology` returns edges and faces with an **index**, and "An edge midpoint is the `[x,y,z]` that picks that edge for fillet and chamfer"; a face's `point` or `name` picks a face (docs/mcp.md). So the agent selects by coordinates/index, not by semantic queries. *Inference:* these picks can break when a parameter changes.
- **Quality claim.** "75 of 76 public reference parts match their analytic or FreeCAD-computed volume, area, centre of mass and bounding box" (README). Self-described "Early but usable for simple parts".
- **License.** MIT OR Apache-2.0, "clean-room contribution rules" (README).

**Lessons for coalesce.**
1. SolidCraft proves strong demand (7k stars in two weeks) for an open, scriptable, headless parametric B-rep CAD with MCP. It has the human UI that coalesce does not plan; coalesce must win on agent ergonomics: semantic selectors, requirements checks, labeled views.
2. Good ideas to copy: one command registry shared by UI, scripts and MCP; per-feature error/warning/timing in the timeline inspection; MCP tool annotations and path sandboxing.

## 8. Other agent-first CAD products and startups (2024–2026)

Mostly closed products; details come from their own sites and press, not tested.

- **Adam (adam.new)** — YC company; $4.1M seed led by TQ Ventures, announced 2025-10-31 (https://adam.new/blog/seed). Open-source CADAM is OpenSCAD-based (section 4). The search summary says the commercial product targets parametric mechanical design (TechCrunch, https://techcrunch.com/2025/10/31/yc-alum-adam-raises-4-1m-to-turn-viral-text-to-3d-tool-into-ai-copilot/, not opened). Kernel of the commercial product: not found.
- **Backflip AI** — "Our revolutionary foundation model creates parametric 3D models with full feature trees, editable in your favorite CAD package"; inputs STL/OBJ/GLB/GLTF/PLY mesh; outputs "Parametric native feature tree or .STEP"; "fast or agentic thinking modes"; "Optimized for 3-axis CNC and turned parts" (https://www.backflip.ai/). $30M Series A, NEA and a16z, December 2024 (https://techcrunch.com/2024/12/19/backed-by-a16z-and-nea-backflip-raises-30m-series-a-to-turn-text-into-ai-generated-designs/, from search results, not opened). Focus: scan/mesh to parametric CAD (reverse engineering), not an agent API.
- **Spectral Labs SGS-1** — "the first generative model for structured CAD", announced 2025-09-15. "Given an image or a 3D mesh, SGS-1 can generate CAD B-Rep parts in STEP format"; compared against GPT-5 and HoLa BRep (https://www.spectrallabs.ai/research/SGS-1). A generator model, not a tool API.
- **Autodesk Fusion MCP / Autodesk Assistant** — per search results, Autodesk has an official local Fusion MCP that lets an AI client "inspect, modify, and execute commands in a running Fusion session", plus cloud Data and Compute MCP servers (https://help.autodesk.com/view/fusion360/ENU/?guid=FMCP-OVERVIEW, https://www.autodesk.com/products/fusion-360/blog/introducing-the-fusion-mcp-opening-fusion-to-ai-powered-workflows/). **Not verified:** the help page returned only a JS shell and the blog returned HTTP 403.
- **Leo AI** — mechanical-engineering knowledge assistant, $9.7M total (https://www.getleo.ai/blog/leo-ai-raises-9-7m-to-build-the-world-s-first-ai-for-mechanical-engineering, from search results). Not a geometry tool.
- **dingcad.com** — agent-first hosted JSON-graph protocol on a mesh kernel. See the separate note `dingcad.md`.
- **CADGenBench** — public leaderboard "for AI-driven CAD generation: how well can a system turn a textual or visual description of a mechanical part into a valid, geometrically correct 3D model?"; submissions are STEP/BREP files; Apache-2.0; created 2026-04-22 (https://huggingface.co/spaces/HuggingAI4Engineering/CADGenBench). Not a product, but the scoreboard agentcad and build123d-mcp cite.
- Not found / not checked: CADstrom, Henqo (names from search; no verified source found in this session).

**Lessons for coalesce.**
1. Funded startups mostly bet on *generator models* (text/image/mesh → STEP or feature tree). Coalesce bets on *tools for a general agent*. These are complementary: a generator's STEP could be an import into coalesce. The tool-for-agents space is mostly open source and small (agentcad, build123d-mcp) or owned by incumbents (Zoo, Onshape, Autodesk).
2. Backflip and Zoo both list "reverse engineer STEP/mesh into editable parametric model" as a goal. *Inference:* coalesce can skip this at first; it is a model-training problem, not a tool problem.

## 9. Comparison table

| Tool | Kernel | Model representation | Sub-shape references | Agent feedback | Requirements checks | Export | License | Activity (2026-10) |
|---|---|---|---|---|---|---|---|---|
| Zoo (KCL, Zookeeper, zoo-mcp) | own B-rep, cloud, closed | KCL code | tags (`$tag`, `body.faces.tag`); engine index also exposed | errors+lint, ortho multi-view snapshots, mass props, face/edge queries | not found | STEP, many (convert tool) | app/MCP MIT; engine proprietary | very active, funded |
| CadQuery | OCCT B-rep | Python (fluent, hidden stack) | direction/type string selectors | Python exceptions | no | STEP, STL, 3MF and more (not checked) | Apache-2.0 | active, 5.9k stars |
| build123d | OCCT B-rep | Python (builder or algebra) | ShapeList filters + op history (`Select.NEW/LAST`, `topo_path`) | Python exceptions | no | STEP, STL, 3MF, glTF, DXF, SVG | Apache-2.0 | active, 3.5k stars |
| OpenSCAD (+CADAM) | mesh CSG (CGAL/Manifold) | .scad code | none | compiler errors, renders | no | STL, DXF (CADAM) | GPL | active; CADAM 5.3k stars |
| Onshape FeatureScript MCP | Parasolid (unverified) | feature list + FeatureScript | historical + state queries (`qCreatedBy`) | FeatureScript errors | not found | full commercial set | proprietary | official, early access |
| build123d-mcp | OCCT | build123d code in a session | recognized feature handles (`@feature[...]`), protected refs | render, measure, feature finding, printability, validate | partial (validate, protected refs on edit) | STEP, STL, DXF, SVG | Apache-2.0 | active, 121 stars |
| agentcad | OCCT | build123d scripts, versioned | "addressable IDs" from inspect (not read in depth) | JSON metrics, 4-view render, diff, viewer, review comments | yes (`spec.json`, `check-spec`, limited kinds) | STEP, STL, GLB, OBJ | Apache-2.0 | active, 168 stars |
| SolidCraft | truck B-rep (Rust) | parametric timeline, JSON command scripts | index, edge midpoint, face point/name | inspect timeline (errors per feature), measure, topology, screenshots | not found | STEP, IGES, 3MF, STL, OBJ, glTF | MIT/Apache-2.0 | very new, 7.4k stars |
| FreeCAD MCP (neka-nat) | OCCT (via FreeCAD) | FreeCAD document + Python | via code | screenshot by default, object list, FEM | no | via FreeCAD | MIT | active, 3k stars |
| Blender MCP | mesh | Blender scene + Python | via code | multi-view `look`, scene info | no | via Blender | MIT | very active, 31.7k stars |
| dingcad.com | mesh (Manifold) | JSON op graph, stable node ids | part/node ids; `decompose[index]` | async browser render, JPEG camera | no | STL | none (repo) | small, solo author |
| Backflip / SGS-1 / Adam | B-rep output (Backflip, SGS-1) | generated feature tree / STEP | n/a | n/a (generators) | n/a | STEP | proprietary | funded startups |

## 10. Gaps no tool fills (where coalesce can lead)

Each gap is "not found" in the sources above; this is a judgment from that survey, not proof that no tool anywhere does it.

1. **Typed feature graph as the source of truth, with an agent API over it, on an open kernel.** Zoo has code (KCL) on a closed cloud kernel; SolidCraft has a timeline on `truck` but coordinate/index picks; dingcad has a typed graph but mesh only; build123d tools all treat a Python script as the truth. No open tool combines a typed, validated graph with OCCT B-rep.
2. **Semantic selectors that survive edits, for agents.** Onshape has the right model (historical + state queries) but behind FeatureScript and a SaaS. build123d now records op history (`Select.NEW/LAST`), but no tool turns it into persistent, named selectors like "faces created by `hole_pattern_1`" that are stored in the model and re-resolved after each edit, with an error when a selector resolves to zero or to an unexpected count.
3. **Requirements-as-tests checked on every edit.** agentcad's `check-spec` is the nearest, but it is a separate step with limited kinds ("cylindrical-feature and structure requirements"). build123d-mcp checks only on its `edit_feature`. Nothing found runs a user/agent-declared test suite (min wall, clearance, hole diameter, mass, fits-in-envelope, printability) automatically inside every commit and returns pass/fail with the failing feature id.
4. **Labeled views.** Zoo, FreeCAD MCP, Blender MCP, SolidCraft and agentcad render images; Zoo can highlight edges. None found draws feature names / selector ids / dimensions onto the image so the agent can link pixels to graph nodes.
5. **Transactional edits for agents.** dingcad.com has `expectedRevision` + `requestId`; SolidCraft has undo/redo. No B-rep tool found offers revisioned, idempotent, atomic multi-edit commits with a diff of geometry and checks in the response.
6. **Structured "why did this fail" errors tied to graph nodes.** dingcad.com's `{code, message, nodeId, actual, limit}` is the best shape seen; OCCT-based tools mostly return Python tracebacks.

## 11. Threats (who is closest to the coalesce idea)

1. **agentcad** (https://github.com/jdilla1277/agentcad) — closest in spirit and stack: build123d, agent-first CLI+MCP, JSON outputs, requirements spec + checks, version diffs, multi-view render, benchmark-driven. Missing (from its README): typed graph as truth (script is truth), semantic persistent selectors (not described). Small (168 stars) but moving fast.
2. **build123d-mcp** (https://github.com/pzfreo/build123d-mcp) — same kernel, strong measurement/feature-recognition/validation tools, `edit_feature` with protected references, MCP registry presence. Missing: a graph source of truth; requirements as persistent tests.
3. **Zoo** (https://zoo.dev) — the best-resourced full stack: own kernel, language, app, agent, MCP, and docs search. Threat if they add requirements checks and a graph-level edit API. Weakness for coalesce to exploit: closed cloud kernel, needs an API token, code (not a typed graph) as truth.
4. **SolidCraft** (https://github.com/tangency-labs/solidcraft) — open, permissive license, B-rep, parametric timeline, headless MCP, huge early traction. If it adds semantic selectors and checks, it overlaps most of coalesce plus a human UI. Weakness today: `truck` kernel maturity (self-described "early"), coordinate/index picks.
5. **build123d itself** — its new operation-history selectors (`Select.NEW/LAST`, `topo_path`) move toward semantic selection in the library. This is an opportunity (coalesce builds on it) and a risk (a thin MCP over build123d may become "good enough").
6. **Onshape / Autodesk** — official MCPs from incumbents. Onshape already has the strongest query model. They target existing customers and cloud/desktop products, not an open Python SDK, so direct overlap is lower.

## Not verified / limits of this note

- Benchmark numbers claimed by agentcad and build123d-mcp (CADGenBench) were not checked against the leaderboard.
- Autodesk Fusion MCP details come only from search summaries (pages blocked).
- Onshape kernel (Parasolid) and REST feedback details were not checked in this session.
- The academic paper numbers in section 2 come from search summaries, not the PDFs.
- Whether build123d's `Select.NEW/LAST` and `topo_path` are in the released PyPI 0.13.0 or only in the dev docs was not checked.
- CadQuery's exact export list was not checked (marked in the table).
- Community MCP servers were judged from READMEs and descriptions only; none were installed or run.
- FreeCAD's own topological-naming work (FreeCAD 1.0) was not covered.
