# coalesce: choosing the first part domain

Status: complete (first pass). Sections 1-2 are cross-domain facts; 3 is per domain; 4-6 are the decision.

Legend: [fact] has a URL. [inference] / [opinion] is my judgment, not sourced.

## 1. Kernel support check (build123d / OCCT)

### Sheet metal
- [fact] build123d has a full sheet metal module in the `latest` (dev) docs, version `0.13.1.dev235`: `BuildSheet`, `SheetMetalParameters` (thickness, bend radius, K-factor), and operations `flange`, `bend`, `jog`, `hem`, `miter`, `corner_relief`, `bend_relief`, holes/cutouts across bends, `unfold`, `thicken`, `sheet shells`. Each operation page lists "the rules it enforces and what it refuses". https://build123d.readthedocs.io/en/latest/sheet_metal/index.html
- [fact] `unfold` develops each bend at the neutral radius from thickness and K-factor, returns a planar `Shell` plus a `FaceMap` (per face: `is_bend`, `angle`, `inside_radius`, `bend_line`, `up`). The docs show DXF export with a `cut` layer and dashed `bend_up` / `bend_down` layers. The face map breaks after a boolean or `clean`. https://build123d.readthedocs.io/en/latest/sheet_metal/unfold.html
- [fact] This is NOT in the released version. PyPI latest is `build123d 0.13.0` https://pypi.org/project/build123d/ ; the `stable` docs (0.13.0) have no Sheet Metal section https://build123d.readthedocs.io/en/stable/ and https://build123d.readthedocs.io/en/stable/sheet_metal/index.html returns 404.
- [fact] OCCT itself has no general "unfold a sheet metal solid" function; FreeCAD's SheetMetal workbench (Python, on OCCT) does this, and exports the unfold sketch to DXF. https://github.com/shaise/FreeCAD_SheetMetal , https://reqrefusion.github.io/FreeCAD-Documentation-html/wiki/SheetMetal_Unfold.html
- [inference] Sheet metal on build123d is new and unreleased. If coalesce chose sheet metal, it would depend on a dev branch, or vendor/port the FreeCAD SheetMetal unfolder (LGPL, tied to FreeCAD `Part` API). That is a real schedule risk, but the risk is falling fast because upstream is actively building it.

### OCCT weak spots (all domains)
- [fact] Fillet and shell (`BRepFilletAPI_MakeFillet`, `BRepOffsetAPI_MakeThickSolid`) can fail or return bad shapes. OCCT documents `MakeThickSolidByJoin` as offset + intersect/round of adjacent faces, so it depends on local geometry; `MakeThickSolidBySimple` is a more limited alternative. https://github.com/Open-Cascade-SAS/OCCT/blob/master/dox/user_guides/modeling_algos/modeling_algos.md
- [fact] A recent OCCT issue: `MakeFillet` on a closed B-spline rim silently returns a self-intersecting solid (so `IsDone()` is not enough; you must run `BRepCheck_Analyzer`). https://github.com/Open-Cascade-SAS/OCCT/issues/1371
- [fact] CadQuery users hit `StdFail_NotDone: BRep_API: command not done` on fillets; a slightly smaller radius can make it pass. https://github.com/CadQuery/cadquery/issues/1553
- [inference] Domains that need many fillets on complex edges plus shells (consumer plastic enclosures, organic printed parts) hit these failures most. Prismatic CNC parts (planar + cylindrical faces, fillets on straight edges) hit them least. Sheet metal avoids general fillet/shell because bends are built as cylinders directly, but it depends on the new unfold code.
- [opinion] For an agent-first tool, a kernel failure is acceptable if coalesce returns a structured error ("fillet r=3 failed on edge `top_outer_edges`, max feasible ~2.4") instead of an exception. That error design matters more than the domain choice.

## 2. Existing open tools for printability / DFM checks

- [fact] Analysis Situs: open-source OCCT-based workbench/SDK. Its open-source feature list includes attributed adjacency graph, drilled hole / cavity / fillet recognition, "Recognize sheet metal features", "Recognize CNC milling features", "Check thickness distribution", shape validity checks, and DXF write. https://analysissitus.org/features.html (C++; not a Python package. License not verified here.)
- [fact] Palmetto (MIT): C++ engine on OCCT 7.8 + Analysis Situs, FastAPI backend, Claude-powered natural-language queries ("find all holes larger than 10mm"). Claims DFM checks for injection molding, CNC (tool access, deep pockets, narrow openings, sharp corners), 3D printing (overhangs, supports), sheet metal (bend radii, hole proximity), casting; ray-based undercut detection with Intel Embree; SDF thickness. https://github.com/connorkapoor/Palmetto (README mirrored at https://github.com/jaideepsingh08/DFM). [inference] Maturity unknown; looks like a single-developer project. It is also a signal that "LLM + OCCT + DFM" is an active idea, i.e. competition.
- [fact] Mesh-level printability checkers exist as small open projects: 3d-print-validator (thin walls, non-manifold, degenerate faces) https://github.com/oriolescserr/3d-print-validator ; 3dprint-doctor (overhangs, thin walls, self-intersections) https://github.com/Zensoro/3dprint-doctor ; printable-mcp (an MCP server) https://github.com/ExaltedTrou6/printable-mcp . Commercial: Meshy has an "analyze printability" API https://docs.meshy.ai/en/api/analyze-printability . [inference] None are standard; trimesh-based overhang/thin-wall checks are easy to rebuild.
- [fact] `BRepExtrema_DistShapeShape` computes minimum distance between two shapes; it is useful for clearance and "no overlaps", but it is not a local wall-thickness tool. https://www.occt3d.com/dev/doc/refman/html/class_b_rep_extrema___dist_shape_shape.html
- [inference] Wall thickness on B-rep is usually done by inward-normal ray casting (OCCT `IntCurvesFace_ShapeIntersector`, or a mesh + BVH) or by SDF/voxels, as Palmetto does. Ray casting gives directional thickness and can miss the true minimum; a robust version needs multiple directions or a medial-axis method. Medium difficulty.

### Export support in build123d (released 0.13.0)
- [fact] The stable Import/Export page lists 3MF, BREP, DXF, glTF, OBJ, STL, STEP, SVG; `Mesher` writes 3MF or STL with metadata; `ExportDXF` / `ExportSVG` for 2D. https://build123d.readthedocs.io/en/stable/import_export.html
- [inference] So STEP, STL, 3MF are free for every domain. DXF flat pattern is only free with the unreleased sheet metal module.

### Standard parts
- [fact] `bd_warehouse.fastener` gives ISO screws and `ClearanceHole(fit="Close"|"Normal"|"Loose")`, `InsertHole` for heat-set inserts. https://bd-warehouse.readthedocs.io/en/latest/fastener.html
- [inference] "M3 clearance holes" can be a typed feature backed by this table, and the check is then exact (hole diameter vs. table), not a guess.

### Adjacent / competing agent-CAD work (context only)
- [fact] Adam (YC) raised a $4.1M seed (Oct 2025) to move a viral text-to-3D tool toward an AI CAD copilot. https://techcrunch.com/2025/10/31/yc-alum-adam-raises-4-1m-to-turn-viral-text-to-3d-tool-into-ai-copilot/ (page body not readable by my fetcher; title confirmed.)
- [fact] Zoo announced "Zookeeper", a conversational CAD agent that works on real geometry. https://docs.zoo.dev/press/zoo-introduces-zookeeper
- [fact] A `build123d-mcp` package exists on PyPI. https://pypi.org/project/build123d-mcp/0.3.67/
- [inference] Plain "LLM writes build123d code" is already crowded. coalesce's wedge must be the verification layer (requirements-as-tests, semantic references), so the domain should be the one where checks are most concrete and most valued.

## 3. Domain analysis

### 3A. 3D-printed functional parts (FDM brackets, mounts, adapters, clips)

**Minimum features.** sketch + extrude/revolve, boolean cut, hole (clearance / insert / counterbore), fillet and chamfer on straight edges, linear/polar pattern, mirror, shell (for hollow parts), text emboss (nice to have). [inference]

**Checks and difficulty.**
| check | how on OCCT/build123d | difficulty |
|---|---|---|
| fits in bounding box / build volume | `BoundBox` | trivial |
| overhang angle vs. build direction | per-face normal sampling (`BRepLProp`), planar faces exact; mesh fallback via trimesh normals | easy |
| min wall thickness | inward-normal ray casting or SDF/voxel (see section 2) | medium; approximate |
| min feature / hole size vs. nozzle | hole recognition from typed features (exact) or cylinder faces | easy if features are typed |
| bridging length | find downward planar faces bounded by supports | medium |
| bed contact / stable base | largest downward planar face area at min Z | easy |
| no overlaps / single solid / watertight | `BRepCheck_Analyzer`, solid count | easy |
| fastener fit (M3 clearance, insert hole) | compare to `bd_warehouse` tables | easy |
| support volume / print time (ground truth) | run PrusaSlicer CLI headless | easy to integrate, slow-ish |
Sources: FDM rules of thumb (overhang ~45 deg, wall ~3 extrusion widths) are widely used but I found no single authoritative standard; heat-set insert guidance example https://asked3d.com/answers/m3-heat-set-insert-hole-size ; PrusaSlicer CLI (`--export-gcode`, `--support-material`, `--info`), marked "no longer maintained" in the wiki https://github.com/prusa3d/PrusaSlicer/wiki/Command-Line-Interface/d9d16199cb3148d1d16307826c993a476377ac04

**Export.** STL and 3MF (both in build123d stable, above); STEP for re-editing.

**Eval data.** Thingi10K: 10k Thingiverse models; per-model licenses, code Apache-2.0 https://github.com/Thingi10K/Thingi10K/blob/main/README.md . Printables terms: use depends on each model's license; no general scraping right https://www.prusa3d.com/page/terms-of-service-of-printables-com_231249/ . CADPrompt (200 prompts + expert CAD code), Text2CAD (~170k models, ~660k text annotations), CAD-Coder (Text-to-CadQuery) exist but do not test manufacturing requirements https://proceedings.iclr.cc/paper_files/paper/2025/file/81a934cd364e18ea6fdeaf57a93c17d4-Paper-Conference.pdf , https://huggingface.co/datasets/gudo7208/CAD-Coder . [inference] Meshes from Printables give targets but no written requirements; you must author requirements per task. Many "remix" and "parametric" models carry their requirements in the description (e.g. "fits 2020 extrusion, M5").

**Demand / who pays.** Hobbyists and makers are the largest, most vocal group; Adam targets them first (TechCrunch link above). Protolabs 3D printing revenue FY2025 ~$80.3M (via search summary of the Protolabs Q4 2025 release; the page returned 403 to me, so not directly verified) https://investors.protolabs.com/news-releases/news-release-details/protolabs-reports-financial-results-fourth-quarter-and-full-6/ . Bambu reported 310k MakerLab users making 2.6M MakerLab models in 2025 (not MakerWorld totals) https://www.prismnews.com/hobbies/3d-printing/bambu-lab-reports-310000-makerlab-users-26m-models-in-2025-400k-lithophanes . [opinion] High usage, low willingness to pay per user; hardware startups and labs pay for speed.

**OCCT risk.** Medium. Simple prismatic brackets are safe; "nice" printed parts use many fillets and shells, which are OCCT's weakest operations (section 1).

**Differentiator fit.** High. Every check is visible and fast to verify; the user can print the result the same day. Semantic refs ("the mounting face", "the clip tab") matter. [opinion]

### 3B. CNC-machined parts (3-axis prismatic: plates, brackets, blocks, housings)

**Minimum features.** extrude, pocket (cut with depth), hole (drilled, tapped, counterbore/countersink with standard sizes), fillet (internal vertical corner radii), chamfer, pattern, mirror. Draft not needed. [inference]

**Checks and difficulty.**
| check | how | difficulty |
|---|---|---|
| internal vertical corner radius >= tool radius | concave edges / cylindrical faces in pockets (AAG dihedral angle); Protolabs states internal corners get the cutter radius https://www.protolabs.com/services/cnc-machining/cnc-milling/design-guidelines/ | easy-medium |
| tool access from setup directions (no undercuts) | visibility / ray casting per direction; Palmetto uses Embree ray casts (section 2) | hard to do well |
| pocket depth / tool diameter ratio | pocket recognition (Analysis Situs cavities) | medium |
| thin walls (Protolabs flags <= 0.020 in / 0.51 mm) https://www.protolabs.com/resources/design-for-machining-toolkit/ | thickness analysis | medium |
| hole depth/diameter, standard drill & tap sizes | typed hole features | easy |
| stock size fits | bounding box | trivial |
Note: with a typed feature graph, most checks read feature parameters directly, avoiding feature recognition. [inference] That is a strong coalesce advantage here.

**Export.** STEP is the required format; shops quote from STEP. [inference; common practice, not separately sourced]

**Eval data.** MFCAD++: labeled STEP models with machining-feature labels on faces (synthetic) https://pure.qub.ac.uk/en/datasets/mfcad-dataset-dataset-for-paper-hierarchical-cadnet-learning-from/ . ABC: ~1M Onshape models, mostly mechanical https://deep-geometry.github.io/abc-dataset/ . Neither has written requirements. [inference] Real CNC parts with requirements are mostly proprietary.

**Demand / who pays.** Biggest money. Protolabs FY2025: CNC $243.3M of $533.1M total (largest service), injection molding $191.5M, 3D printing $80.3M, sheet metal $17.2M (search summary of Protolabs release, not directly fetched) https://investors.protolabs.com/news-releases/news-release-details/protolabs-reports-financial-results-fourth-quarter-and-full-6/ . Xometry FY2025 revenue $686.6M, marketplace $629.6M; CNC share not disclosed https://investors.xometry.com/news-releases/news-release-details/xometry-reports-record-fourth-quarter-and-strong-full-year-2025 . Payers: hardware startups, engineering teams, job shops (quoting/DFM). [inference]

**OCCT risk.** Lowest. Prismatic geometry (planes, cylinders, straight-edge fillets) is where OCCT is most reliable. [inference]

**Differentiator fit.** High for semantic refs (pockets, bores, datum faces) and typed holes. Medium for the agent loop: the hard checks (tool access) are expensive and approximate, and the user cannot verify cheaply at home. [opinion]

### 3C. Sheet metal (laser-cut + press-brake brackets, trays, covers)

**Minimum features.** base flange from sketch, edge flange, bend, bend relief, corner relief, hem, cutouts (incl. across bends), unfold to flat pattern, thicken. All exist in build123d dev docs (section 1). Fillet and shell are NOT needed.

**Checks and difficulty.**
| check | how | difficulty |
|---|---|---|
| inside bend radius >= material minimum | parameter on the bend feature | trivial |
| min flange length (material/thickness table; e.g. 0.250 in 5052 Al needs 1.150 in flat flange at SendCutSend) https://sendcutsend.com/materials/processing-min-max/ | feature parameter | trivial |
| hole/cut distance from bend line (SendCutSend: features within 0.315 in of bend centerline may distort) https://sendcutsend.com/guidelines/bend-deformation/ | distance from cut edges to `bend_line` in face map | easy |
| relief present where bend ends inside sheet | topology of bend ends | easy (build123d ops "enforce rules and refuse") |
| flat pattern self-overlap / fits sheet | 2D boolean on unfolded shell | easy |
| bend sequence / tool collision on brake | needs press-brake simulation | hard; out of v1 scope |
**Export.** DXF flat pattern with bend lines (documented in build123d dev), plus STEP. https://build123d.readthedocs.io/en/latest/sheet_metal/unfold.html

**Eval data.** Small. 26 STEP aerospace sheet metal parts on Zenodo https://zenodo.org/records/6970030 ; small GitHub collections with folded + flat DXF https://github.com/N1-Conception/SolidWorks-SheetMetal-Designs . [inference] Not enough real parts with requirements; you would author tasks.

**Demand / who pays.** SendCutSend is private; reported cumulative revenue passed $100M by Oct 2025 (secondary source) https://open-factory.com/articles/sendcutsend/ . Protolabs sheet metal is small ($17.2M, above). Protocase sells custom sheet metal and machined electronic enclosures and gives away "Protocase Designer" with manufacturability checks and instant pricing https://www.protocase.com/products/services/design/ . Payers: hardware startups, robotics teams, job shops. [inference]

**OCCT risk.** Low for geometry (cylinders + planes), HIGH for dependency: the sheet metal module is unreleased (section 1). Fallback is porting FreeCAD SheetMetal unfold logic.

**Differentiator fit.** Medium-high. Requirements are crisp and tables exist (great for requirements-as-tests). But checks are so rule-based that a smart parametric template (Protocase Designer) already covers much of the value; the "agent loop" adds less. [opinion]

### 3D. Electronics enclosures around a PCB (first process: FDM; later sheet metal / CNC)

**Minimum features.** everything in 3A plus: import PCB as a fixed reference body (STEP from KiCad), standoffs/bosses at PCB mounting holes, port cutouts aligned to connector faces, lid split / lip, vents (pattern), snap or screw closure. [inference]

**Checks and difficulty.**
| check | how | difficulty |
|---|---|---|
| PCB fits with clearance to walls | `BRepExtrema_DistShapeShape` between PCB body and enclosure solid | easy |
| no overlap with any component | boolean common / distance > 0 | easy |
| clearance above tallest component | bounding box of component solids vs. lid inner face | easy |
| standoffs coaxial with PCB holes, correct insert/screw size | compare boss axes to PCB hole cylinders; `bd_warehouse` sizes | easy-medium |
| connector cutout aligned and large enough (plug overmold clearance) | project connector face onto wall, compare to opening | medium |
| wall >= 2 mm, overhangs, fits in 100x60x40 | as 3A | easy-medium |
| lid and base do not collide; screw reaches | needs 2 bodies -> touches "assemblies", which v1 excludes | medium; scope risk |
- [fact] KiCad exports board + components to STEP from the CLI (`kicad-cli pcb export step`, options such as `--board-only`, `--no-components`). https://docs.kicad.org/9.0/en/cli/cli.html
- [fact] Existing generators: kicad2freecad-enclosures (standoffs, connector cutouts, vents, lids, "field feedback from real prints") https://github.com/mikeysklar/kicad2freecad-enclosures ; pcb-enclosure-generator (snap-fit, keep-outs, ports; OpenSCAD/manifold) https://github.com/hadencain/pcb-enclosure-generator ; several web generators. [inference] Proves demand and gives baselines to beat, but these are templates, not agents with verification.

**Export.** STL/3MF for printing; STEP for MCAD hand-off and for later sheet metal/CNC versions.

**Eval data.** [inference] Best of all domains: every open-hardware KiCad project is a free task generator. Requirements come out of the PCB file itself (board outline, hole positions, connector positions, component heights), so tests are machine-generated and objective, no human labeling. Count of public KiCad repos: not found (I did not get a number).

**Demand / who pays.** Hobbyist electronics + hardware startups; Protocase is a whole business built on custom electronic enclosures (link above). [opinion] Hardware startups are the most likely payers: they have a PCB, need a case this week, and lack a mechanical engineer.

**OCCT risk.** Medium: shells and fillets on box-like solids are usually fine; risk rises with rounded consumer-style shapes. Vents (many small cuts) can be slow. [inference]

**Differentiator fit.** Highest. Semantic references are natural ("USB-C port on J3", "mounting hole H2"), requirements are concrete and derived from data, renders with labels are easy to judge, and the agent loop has real work (iterate until all tests pass). [opinion]

## 4. Comparison table [opinion; scores are my judgment from the evidence above]

Scale 1 (bad) to 5 (good). Weight in brackets. Max weighted total = 65.

| criterion [weight] | 3A printed parts | 3B CNC prismatic | 3C sheet metal | 3D PCB enclosure (FDM first) |
|---|---|---|---|---|
| kernel readiness / OCCT risk [2] | 4 (fillet/shell risk on organic parts) | 5 (prismatic = OCCT strong) | 2 (sheet metal module unreleased) | 4 (box-like shells usually fine) |
| checks computable & exact [2] | 4 | 3 (tool access is hard) | 5 (mostly parameter rules) | 4 |
| eval data with real requirements [2] | 3 (meshes, requirements hand-written) | 2 (synthetic MFCAD++, no requirements) | 2 (tiny sets) | 5 (KiCad projects generate tests) |
| demand / willingness to pay [2] | 3 (many users, low pay) | 5 (largest $ in Protolabs) | 4 (SendCutSend, Protocase) | 4 (hardware startups, makers) |
| shows coalesce differentiators [3] | 4 | 3 | 3 | 5 |
| fits v1 scope (no assemblies) [1] | 5 | 5 | 5 | 3 (PCB as fixed reference; lid is a 2nd part) |
| room vs. competitors [1] | 2 (Adam et al. target here) | 3 | 3 (Protocase Designer) | 3 (template generators only) |
| **weighted total** | **47** | **47** | **43** | **55** |

## 5. Ranked recommendation

1. **Electronics enclosures around a given PCB, FDM-printed first** (score 55). [opinion]
   - Requirements come from data (KiCad STEP / board file), so requirements-as-tests are objective and the eval set can be generated from public open-hardware projects without human labeling.
   - Semantic references have natural names (connector `J3`, mounting hole `H2`, "lid inner face"), which is exactly what coalesce sells.
   - The feature set is a strict superset of 3A, so nothing is wasted if you widen later.
   - Payers exist: hardware startups without a mechanical engineer; Protocase proves the enclosure business.
   - Scope rule: treat the PCB as a read-only reference body (keep-out + datums), not an assembly. Do the base as one part in v1; the lid as a separate part checked against the base with only a clearance/no-overlap test.
2. **3D-printed functional parts** (47). Same engine as #1 with less setup; best fallback if the PCB-reference idea breaks the "no assemblies" rule. Crowded with text-to-CAD tools, so lead with verification.
3. **CNC prismatic parts** (47, tie broken by lower showcase value and weak eval data). Best money and best kernel fit; make it domain 2 by adding a "CNC process profile" (corner radius, standard holes, stock size) on the same feature graph. Tool-access checking is the expensive part; defer.
4. **Sheet metal** (43). Checks are the easiest of all, but build123d's module is unreleased (`0.13.1.dev`, not in `0.13.0`). Revisit when it ships; then "enclosure in sheet metal" is a natural v2 that reuses #1's PCB-driven requirements and adds DXF flat patterns.

Suggested de-risk spike (1 week) [opinion]: take 10 public KiCad boards, export STEP, have an agent build a base enclosure with typed features, and run 6 checks (fits PCB, no overlap, standoff alignment, port alignment, wall >= 2 mm, overhang). Count OCCT failures in shell/fillet. If more than ~1 in 10 builds hit kernel failures that a structured error cannot explain, move to option 2 with simpler geometry.

## 6. What would change the recommendation

- **Henry wants B2B revenue first** (job shops, engineering teams): pick CNC prismatic. It has the largest spend (Protolabs CNC $243.3M FY2025, unverified directly) and the lowest kernel risk.
- **build123d releases sheet metal and it is stable**, or a fabricator (SendCutSend/Protocase-like) offers a quoting/DFM API partnership: sheet metal moves to #1 or #2, because its checks are exact and its deliverable (DXF) is directly orderable.
- **"No assemblies" is strict** and even a fixed PCB reference counts: pick 3D-printed functional parts.
- **The spike shows frequent shell/fillet failures** on enclosures: pick CNC prismatic or simple printed brackets (fewer shells).
- **A strong open or commercial agent tool already does PCB-to-enclosure with verification**: I did not find one, but I only searched briefly; if it exists, the gap closes and CNC becomes more attractive.

## 7. Not verified / gaps
- Protolabs FY2025 per-service revenue: from a search summary; the press release returned HTTP 403 to my fetcher.
- SendCutSend revenue: secondary source only; private company.
- Adam funding/positioning: TechCrunch title confirmed, body not readable; positioning from search summary.
- Analysis Situs license and Palmetto maturity: not checked.
- Number of public KiCad projects usable as an eval set: not found.
- No authoritative FDM standard for overhang/min wall found; values are community rules of thumb.
- build123d sheet metal release date: not found; only that it is in dev docs and absent from 0.13.0.
