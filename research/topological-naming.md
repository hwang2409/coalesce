# Topological naming for coalesce (research note)

Status: complete (first pass). Research date: about late October 2026, inferred from GitHub
commit dates seen (build123d v0.13.0, OCCT docs 8.0.1).

Scope: how existing CAD systems make face/edge/vertex references survive parameter edits
(the topological naming problem, TNP), and a recommended approach for coalesce on OCCT
(build123d/OCP), with a typed feature graph and semantic selectors for an AI agent user.

Conventions: every claim has a URL. "Inference:" marks my own reasoning. "Not found" means I
looked and did not find a source.

## 1. Onshape (FeatureScript queries)

### Mechanism

- A FeatureScript `Query` is "an order form for geometry": it encodes criteria, not entity
  pointers. It is resolved against a context only when an operation or `evaluateQuery` needs it.
  Onshape says it uses queries instead of direct references "out of a need for robustness in the
  face of changes earlier in the feature list" (example given: find the top face of a cube again
  after a resize and a hole). https://cad.onshape.com/FsDoc/modeling.html
- Every feature and operation has a hierarchical `Id` (for example `id + "extrude1" + "fillet"`).
  Ids must be unique. Queries use these Ids; `ANY_ID` (`"*"`) is a wildcard, and
  `unstableIdComponent` marks a component (such as an index into an evaluated query) as not
  robust so queries treat it as a wildcard. https://cad.onshape.com/FsDoc/library.html#Id
  https://cad.onshape.com/FsDoc/library.html#unstableIdComponent-
- `qCreatedBy(featureId, entityType)`: entities *added* by an operation. Modified-but-not-created
  entities are not returned. Split result entities are "created by" both the original creator and
  the split operation. Merged entities are "created by" all merged creators plus the merging
  operation. https://cad.onshape.com/FsDoc/library.html#qCreatedBy-Id-EntityType
- Role-based queries on top of provenance: `qCapEntity(featureId, CapType)` /
  `qNonCapEntity` (start/end caps vs side faces of extrude, revolve, sweep, loft, thicken);
  `qSplitBy(featureId, entityType, backBody)` (front/back side of a split, defined by the split
  tool normal); `sketchEntityQuery(sketchId, type, sketchEntityId)` (wire bodies of one sketch
  entity); `qPatternInstances`. https://cad.onshape.com/FsDoc/library.html#qCapEntity-Id-CapType-EntityType
  https://cad.onshape.com/FsDoc/library.html#qSplitBy-Id--boolean
  https://cad.onshape.com/FsDoc/library.html#sketchEntityQuery-Id--string
- History tracking: `startTracking(context, sketchId, "line0")` returns a query that later
  resolves to entities *derived from* that sketch entity, e.g. the side face and top/bottom edges
  an extrude made from `line0`. `startTrackingIdentity` follows entities that inherit identity.
  `makeRobustQuery` "generates query robust to identity-preserving changes";
  `makeRobustQueriesBatched(..., followSplitMerge)` optionally follows splits and merges.
  https://cad.onshape.com/FsDoc/library.html#startTracking-Context-map
  https://cad.onshape.com/FsDoc/library.html#makeRobustQuery-Context-Query
- Order is a weak point that Onshape documents openly: `qNthElement` order is "deterministic but
  arbitrary"; `evaluateQuery` order is "arbitrary (and generally not predictable)" except for
  `qUnion`. `evaluateQuery` returns *transient* queries that "may become invalid" once the context
  changes. https://cad.onshape.com/FsDoc/library.html#qNthElement-Query-number
  https://cad.onshape.com/FsDoc/library.html#evaluateQuery-Context-Query
- Geometric/topological filters also exist (`qContainsPoint`, `qParallelPlanes`, adjacency, etc.)
  and compose with provenance queries. https://cad.onshape.com/FsDoc/modeling.html
- User picks in the UI are stored in feature JSON as `BTMIndividualQuery-138` with
  `deterministicIds` (e.g. `["JCC"]`) or alternatively a `queryString`. Docs say IDs "should be
  obtained from the Part Studio, not hard-coded".
  https://onshape-public.github.io/docs/api-adv/featureaccess/
- Onshape "does not expose a persistent ID" for faces/edges; IDs may change. Instead it offers an
  `idtranslations` REST call that maps old IDs (from a source microversion) to new IDs with a
  status per ID: `OK`, `SPLIT` (one-to-many target list), `FAILED_TO_RESOLVE` (empty target).
  https://onshape-public.github.io/docs/api-adv/associativity/
- When a reference cannot resolve, the feature errors and the user uses the Repair workflow to
  pick a replacement (and can propagate it downstream).
  https://cad.onshape.com/help/Content/Document/repairing.htm

### What survives

- Parameter edits upstream: provenance queries (`qCreatedBy`, `qCapEntity`) re-resolve each
  regen because they key on operation Ids, not entity indexes (inference from the docs above).
- Splits/merges: tracked explicitly, both in `qCreatedBy` semantics and in `idtranslations`
  `SPLIT` status. https://cad.onshape.com/FsDoc/library.html#qCreatedBy-Id-EntityType
- Feature reorder: Ids do not change on reorder, so provenance queries still name the same
  feature (inference). If the referenced feature moves after the referencing one, the reference
  cannot resolve (inference; general history-based behavior).

### Failure modes

- Index-style queries (`qNthElement`) are explicitly arbitrary in order.
- The internal algorithm that maps `deterministicIds` across regenerations is not public. Not
  found: any Onshape paper or doc describing it.
- Ambiguity becomes a visible feature error, not a silent wrong pick (from Repair docs).

### Cost to copy

- The *query language* part is cheap to copy: a small typed AST of provenance + role + geometric
  predicates. The *deterministic ID* part needs a per-operation history from the kernel (Parasolid
  in Onshape's case). On OCCT, the equivalent input is `BRepTools_History` (see section 3).
- Design lessons for coalesce (inference): (a) query = criteria, resolved per regen;
  (b) role queries (cap/side/split side/derived-from-sketch-entity) are the core vocabulary;
  (c) resolution returns a status (OK / SPLIT / FAILED), never a silent best guess;
  (d) mark unstable components explicitly.

## 2. FreeCAD 1.0 (realthunder's toponaming / element map)

### Mechanism

- FreeCAD runs on OCCT. Its TNP mitigation was developed by realthunder in the LinkStage3 fork
  and merged in pieces; FreeCAD 1.0 is the first stable release with the new naming algorithm.
  https://wiki.freecad.org/Topological_naming_problem
  https://www.ondsel.com/blog/freecad-topological-naming/
- `Part::TopoShape` wraps an OCCT `TopoDS_Shape` and carries an *element map*: index names
  (`Face6`, `Edge5`) map to long *mapped names* that encode history. Source today:
  `src/Mod/Part/App/TopoShapeExpansion.cpp` (`makeElementShape`, `makeShapeWithElementMap`,
  `getRelatedElementsCached`), `src/App/ElementMap.cpp`, `src/App/MappedName.h`.
  https://github.com/FreeCAD/FreeCAD/blob/main/src/Mod/Part/App/TopoShapeExpansion.cpp
  https://github.com/FreeCAD/FreeCAD/blob/main/src/App/ElementMap.cpp
- Input to naming is OCCT's own history: `BRepBuilderAPI_MakeShape::Generated/Modified` and
  derivatives. Because not all OCCT makers share one interface, FreeCAD uses an abstract
  `TopoShape::Mapper`; special cases include `BRepBuilderAPI_Sewing` and
  `BRepOffsetAPI_ThruSections`. There is also a `MapperHistory` that wraps `BRepTools_History`,
  and a `ShapeMapper` with `MappingStatus::{Generated, Modified}` for hand-written mappings.
  https://github.com/realthunder/FreeCAD_assembly3/wiki/Topological-Naming-Algorithm
  https://github.com/FreeCAD/FreeCAD/blob/main/src/Mod/Part/App/TopoShapeMapper.h
- Naming algorithm (`makESHAPE`, four passes):
  1. Copy names of elements that are unchanged between input and output.
  2. Name generated (`;:G`) and modified (`;:M`) elements from the mapper. The name holds the
     first source element name, a generation/modification index, other sources in brackets, the
     op code (e.g. `FUS`, `XTR`, `RFI`), the owner object tag `;:T<tag>`, the source name length,
     and the element type. Example: `Face6;:M2;FUS;:T1:5:F` = face modified from Face6 of
     object 1 by a fusion.
  3. Reverse pass: unnamed lower elements are named from a named upper element
     (`F1;:U1`, `F1;:U2`).
  4. Forward pass: unnamed upper elements are named from the set of their named lower elements
     (`(edge1,edge2,edge3);:L`).
  Long names are shortened with a segment-wise string hasher (`#a8;:M#a7;RFI;:T2:2:F`).
  https://github.com/realthunder/FreeCAD_assembly3/wiki/Topological-Naming-Algorithm
- References (`PropertyLinkSub`) store the mapped name plus the old index name, e.g.
  `Edge5;:T1:5:E.Edge5`. After a change the edge index went from 5 to 9 but the mapped name
  stayed `Edge5;:T1:5:E`. https://github.com/realthunder/FreeCAD_assembly3/wiki/Topological-Naming-Algorithm
- Recovery: `getRelatedElements()` searches for elements whose names start with
  `<missing name>;:M` or `<first source>;:M`. `getElementHistory()` decodes a name back to the
  first source object and element. A broader `Part.getRelatedElement()` traces every element's
  history to find ones with the same initial source. Same source.

### Stated purpose and limits (from the FreeCAD wiki)

- Three goals, in priority order: (1) detect broken references and show an error at the
  operation that changed names; (2) propose a likely replacement (common for fillet/chamfer);
  (3) auto-repair only when confidence is high ("First, do no harm").
  https://wiki.freecad.org/Topological_naming_problem
- Auto-repair of sketch-on-face is "frequently (but not always)" correct for parametric changes,
  and "less likely" when operations are added or removed in the middle of a body. The wiki still
  recommends datum planes and attaching sketches to origin planes.
  https://wiki.freecad.org/Topological_naming_problem
- Split ordering: when one element becomes several, the modification index follows the order
  OCCT reports, so a fillet can silently move to the "first" piece (realthunder's own example:
  the fillet moved to the lower edge). Adding vertex names would not fully fix it for 3+ pieces.
  https://github.com/realthunder/FreeCAD_assembly3/wiki/Topological-Naming-Algorithm
- Guessing a replacement (`getRelatedElements`) is "not always reliable" and is used only in the
  edit dialog, not inside the fillet feature. It works poorly when the change is several steps
  back in history. Same source.
- Overhead (one informal test, 148 objects): recompute 40 s -> 52 s, file 17.8 -> 22.6 MiB
  uncompressed. Same source.

### Cost to implement

- Very high in FreeCAD's case: "many, if not most, files in FreeCAD need to be modified"; the
  merge was split into phases over years.
  https://www.ondsel.com/blog/freecad-topological-naming/
- Inference: for a new Python project with a small, closed set of operations, the core idea
  (names derived from OCCT generated/modified history plus per-op role tags) is much cheaper,
  because every op can be written history-aware from day one. The cost is per-operation glue
  for each OCCT algorithm whose history API is incomplete.

### Lessons for coalesce (inference)

- Reuse the shape of the idea: element identity = (creating feature, op, source element, role,
  index-among-siblings), derived from OCCT history.
- Do not copy the opaque string encoding; a typed record is easier for an agent and for tests.
- Do not trust sibling order on splits; resolve split pieces with a geometric/semantic
  tie-breaker, or error.
- Copy the priority order: detect and report first, suggest second, auto-repair last.

## 3. OCCT's own tools

### 3a. Per-algorithm history: Generated / Modified / IsDeleted

- Every `BRepBuilderAPI_MakeShape` subclass exposes `Generated(S)`, `Modified(S)`, `IsDeleted(S)`.
  https://occt3d.com/dev/doc/refman/html/class_b_rep_builder_a_p_i___make_shape.html
- Sweeps add role access: `BRepPrimAPI_MakePrism::FirstShape()` / `LastShape()` return the
  bottom and top (cap) of the prism, also per sub-shape (`FirstShape(S)`).
  https://occt3d.com/dev/doc/refman/html/class_b_rep_prim_a_p_i___make_prism.html
- `BRepFilletAPI_MakeFillet::Generated(EorV)` gives faces made from an edge or vertex;
  `Modified(F)` gives trimmed faces.
  https://occt3d.com/dev/doc/refman/html/class_b_rep_fillet_a_p_i___make_fillet.html
- Booleans: "All operations in Boolean Component support History information", also for solids.
  `Generated` is only filled for pure intersections: vertices generated from edges/faces, edges
  generated from faces. A split edge or face is reported as `Modified` into its pieces. A tool
  face fully absorbed is `Deleted`. The General Fuse algorithm "has a history-based architecture
  designed to allow using OCAF naming functionality".
  https://occt3d.com/dev/doc/overview/html/specification__boolean_operations.html
- History collection in `BRepAlgoAPI_BuilderAlgo` can be enabled or disabled ("History
  support ... Enabling/Disabling the history collection"); inference: it has a cost and must be
  left on in coalesce.
  https://occt3d.com/dev/doc/refman/html/class_b_rep_algo_a_p_i___builder_algo.html
- `SimplifyResult` (which runs `ShapeUpgrade_UnifySameDomain` to merge tangent/coplanar faces and
  edges) merges its history into the boolean's history.
  https://occt3d.com/dev/doc/overview/html/specification__boolean_operations.html
  `ShapeUpgrade_UnifySameDomain` also exposes `History()` returning a `BRepTools_History`.
  https://occt3d.com/dev/doc/refman/html/class_shape_upgrade___unify_same_domain.html

### 3b. BRepTools_History

- A general container of three relations from input sub-shapes S to output sub-shapes T:
  generated `T <= G(S)`, modified `T <= M(S)`, removed `R(S)`. Only vertex, edge, face and solid
  are tracked. A shape cannot be both removed and modified; generated and modified sets do not
  overlap; shapes can be generated from a removed shape (fillet: edges generate faces, then are
  removed). Each output must be an input or come from one.
  https://occt3d.com/dev/doc/refman/html/class_b_rep_tools___history.html
- Two histories H12 and H23 can be merged into H13 (with documented rules, e.g. generated then
  modified = generated). This is the tool for chaining the history of a multi-step feature.
  Same source.
- Template constructor builds a history from any algorithm with `IsDeleted/Modified/Generated`.
  Same source.
- Guidance for "modified" vs "generated" is a recommendation, not enforced: e.g. "modified" if
  it replaces the input with changes of location, tolerance, bounds, parametrization. Same source.
  Inference: different algorithms classify edge cases differently; coalesce must test each op.
- Unchanged sub-shapes: when an operation does not touch a sub-shape, the output contains the
  same `TopoDS` entity (FreeCAD's naming step 1 relies on this: "Find any unchanged and named
  elements that appear in both the input and out shape").
  https://github.com/realthunder/FreeCAD_assembly3/wiki/Topological-Naming-Algorithm
  build123d's `ShapeHistory` also defines "untouched (it is still there, unchanged)" as a state.
  https://github.com/gumyr/build123d/blob/dev/src/build123d/topology/history.py

### 3c. TNaming / OCAF naming

- OCAF stores shapes on labels as `TNaming_NamedShape` attributes, each a list of (old, new)
  shape pairs with one evolution: `PRIMITIVE`, `GENERATED`, `MODIFY`, `DELETE`, `SELECTED`
  (enum also has `REPLACE`). All shapes live in one `TNaming_UsedShapes` map at the root.
  https://occt3d.com/dev/doc/overview/html/occt_user_guides__ocaf.html
  https://occt3d.com/dev/doc/refman/html/_t_naming___evolution_8hxx.html
- The naming mechanism has three parts: algorithm history; loading that history into the
  document under a "Result Label" in a fixed linear order; and selection/recompute via
  `TNaming_Selector` (`Select()` stores a name with evolution `SELECTED`; `Solve()` recomputes
  it after upstream changes and "fails" if the name is not correct). The history must cover:
  for a solid, all faces; for an open shell, faces plus open-boundary edges; for wires, edges
  (+ end vertices); seam edges must be tracked; degenerate entities excluded.
  https://occt3d.com/dev/doc/overview/html/occt_user_guides__ocaf.html
- The stored "name" is a recipe of name types: `IDENTITY`, `MODIFUNTIL`, `GENERATION`,
  `INTERSECTION` (e.g. an edge as the intersection of its two faces), `UNION`, `SUBSTRACTION`,
  `CONSTSHAPE`, `FILTERBYNEIGHBOURGS`, `ORIENTATION`, `WIREIN`, `SHELLIN`.
  https://occt3d.com/dev/doc/refman/html/_t_naming___name_type_8hxx.html
  The OCAF guide says the Naming attribute holds e.g. "the faces to which a selected edge is
  common to" and that after a parameter change "the reference-keys still address the
  appropriate faces". https://occt3d.com/dev/doc/overview/html/occt_user_guides__ocaf.html
- Requirement: each modeling function must register history in the exact expected order on
  each regen ("the work of the three components should be synchronized and the rules of each
  component should be respected"). Same source.

### What survives, failure modes, cost

- History gives exact answers *within one run*: which result face came from which input
  face/edge. It does not by itself give identity *across two runs* (regen after a parameter
  edit). Across runs you need a key that does not depend on `TopoDS` pointers: the feature id
  plus the source element's own stable key (recursive), plus role. Inference, consistent with
  how FreeCAD (section 2) and TNaming (labels in fixed order) both work.
- Failure modes: split pieces come back in algorithm order (see FreeCAD split example);
  algorithms disagree on generated vs modified; some algorithms raise when asked about a
  sub-shape kind they do not track (build123d notes `BRepFilletAPI_MakeFillet2d` "raises
  Standard_Failure on one build and Standard_TypeMismatch on another").
  https://github.com/gumyr/build123d/blob/dev/src/build123d/topology/history.py
  Sewing and lofts need special handling (FreeCAD names `BRepBuilderAPI_Sewing` and
  `BRepOffsetAPI_ThruSections`).
  https://github.com/realthunder/FreeCAD_assembly3/wiki/Topological-Naming-Algorithm
- TNaming cost: it requires an OCAF document (TDF labels/attributes) as the data model, which
  conflicts with coalesce's own typed feature graph as source of truth (inference). Not found:
  a Python CAD project that uses TNaming in production through OCP. FreeCAD, the largest OCCT
  app, built its own element map on top of raw history instead (section 2). Inference: use
  `BRepTools_History` directly; skip TNaming, but borrow its name-type ideas (intersection of
  named faces, filter by neighbours) as fallback resolvers.

## 4. build123d and CadQuery selectors

### Mechanism

- Both are code-first: the "reference" is a selector expression in the script, re-evaluated on
  every run. Nothing is stored between runs, so there is no stored name to break; the selector
  itself may pick a different entity after an edit (inference from the APIs below).
- build123d: selectors `vertices()/edges()/faces()/...` return a `ShapeList`; operators
  `sort_by` (`>`/`<`), `group_by` (`>>`/`<<`), `filter_by` (`|`, by `Axis`, `Plane`, `GeomType`,
  `Convexity`, or a lambda), `filter_by_position`, and Python indexing `[]`.
  https://build123d.readthedocs.io/en/latest/selectors.html
- build123d `Select.LAST` / `Select.NEW` (since the history rework, in releases v0.12.0 and
  v0.13.0): read "the record every operation leaves on its result". `LAST` = features the last
  operation brought in or created (a face only trimmed is not included); `NEW` = features that
  existed in no input (intersection edges, fillet faces).
  https://build123d.readthedocs.io/en/latest/topology_selection.html
  Implementation: `topology/history.py` `ShapeHistory` wraps `BRepTools_History`, built from
  boolean `History()`, `UnifySameDomain.History()`, per-algorithm `Generated/Modified`, sewing,
  and relocation (pairing by TShape). Records merge across steps. Commit "Select.LAST and
  Select.NEW from the kernel's own record" (2026-09-10) replaced before/after shape comparison.
  https://github.com/gumyr/build123d/blob/dev/src/build123d/topology/history.py
  https://github.com/gumyr/build123d/commit/1a7751a1
- CadQuery string selectors: `+Z`, `|Z`, `-X`, `#Z`, `%Plane`, `>Y`, `<Y`, `>Y[-2]`, `>>Y[-2]`,
  combinable with `and/or/not/exc`; topological `ancestors()` / `siblings()`. For non-planar
  faces the selector is evaluated at the center of mass, which "can lead to results that are
  quite unexpected"; non-linear edges are skipped by most string selectors.
  https://cadquery.readthedocs.io/en/latest/selectors.html
- CadQuery `tag(name)` tags a *Workplane stack object* for later reference, not a face/edge.
  https://github.com/CadQuery/cadquery/blob/master/cadquery/cq.py
- CadQuery free-function API (experimental) has an optional `History` object: operations take
  `history=hist, name="hollow"`, and later code uses
  `hist["hollow"].generated().edges("<Z")` or `hist["hollow"].modified().face(">Z")`. This is
  provenance + geometric selector, the same pattern recommended below. Docs warn: "experimental
  and may change". https://github.com/CadQuery/cadquery/blob/master/doc/free-func.rst
  Implementation `_update_history` collects `IsDeleted/Generated/Modified/FirstShape/LastShape`
  from prism, revol, pipe shell, offset, boolean builders.
  https://github.com/CadQuery/cadquery/blob/master/cadquery/occ_impl/shapes.py

### Why they are or are not stable

- Stable when the predicate captures design intent and the intent stays unique: `>Z` on a plain
  box survives a height change (inference).
- Not stable when: (a) an edit adds a new entity that also matches (a boss raises a higher
  face, `>Z` now picks it); (b) Nth/indexing (`>Y[-2]`, `[0]`) depends on how many entities
  exist; (c) ties: `sort_by` on equal values gives an order that is arbitrary (inference;
  not found: a doc that defines tie order); (d) non-planar faces evaluated at centroid (CadQuery
  doc warning above).
- Provenance (`Select.NEW`, CadQuery `History`) is only for *the last op* or *named ops in the
  same run*; it is not persisted. Inference: in coalesce the regen always re-runs the whole
  feature graph, so per-run provenance is enough, *if* each feature's history is kept keyed by
  feature id during the run.
- Silent wrong picks are the main risk: none of these selectors error when the match set
  changes size, except single-result accessors (`face()`/`edge()` "either return a single shape
  or throw" in CadQuery free-func; build123d `get_single_shape`).
  https://github.com/CadQuery/cadquery/blob/master/doc/free-func.rst

### Cost

- Low: pure Python on top of OCP. build123d's `history.py` is a few hundred lines and is a
  direct template for coalesce's per-op history capture.

## 5. Zoo / KCL tags (agent-facing design)

### Mechanism

- KCL is the source of truth of a Zoo model ("KCL *is* the fundamental model underpinning Zoo")
  and Zoo explicitly markets text as good for LLMs.
  https://zoo.dev/research/introducing-kcl
- Faces of a body are named from *sketch segment names*: a wall extruded from segment `bottom`
  of a region is `region001.tags.bottom`. Caps are named with tag declarators at the operation:
  `extrude(..., tagStart = $startCap, tagEnd = $endCap)`, then `body.faces.endCap`. Fillets
  and chamfers can `tag` their generated face for later use.
  https://zoo.dev/docs/kcl-lang/types
  https://zoo.dev/docs/kcl-lang/edge-references
- `START` / `END` special tags exist but "are generally discouraged since they're ambiguous".
  Tags can "leak outside of function scopes and be updated (i.e. re-bound to the face of a
  different body)" for backward compatibility; dot notation on the body is "strongly
  preferred". https://zoo.dev/docs/kcl-lang/types
- Edges are not named directly. An edge reference is an object
  `{ sideFaces = [faceA, faceB], endFaces = [...], index = n }`: side faces = faces along the
  edge; end faces = faces touching its end (used after a split); index = zero-based last
  resort. "Faces are generally more stable and predictable than edges after modeling
  operations." A reference may intentionally match several edges (one fillet on both split
  pieces). https://zoo.dev/docs/kcl-book/edge_references.html
  https://zoo.dev/docs/kcl-lang/edge-references
- Guidance order: sideFaces alone; add endFaces when split; index only when still ambiguous.
  Same source.

### What survives

- Parameter edits to sketch dimensions / extrude length: wall and cap names derive from the
  segment name and the operation's tag, not from geometry, so they persist (inference from the
  naming scheme).
- Split edges: handled by `endFaces`. Same source.
- Feature reorder / insertion: names are lexical variables in code, so they survive as long as
  the referenced variable is still defined before use (inference).

### Failure modes

- `index` is order-based and has the usual risk (doc calls it rare).
- Faces from booleans that are split (one sketch wall cut in two) share one tag; doc examples
  show edges on such faces but I did not find a doc on how a *face* tag resolves when the face
  is split into several. Not found.
- Tag rebinding / leaking across scopes (documented above).
- The Zoo geometry engine is not OCCT; how the engine maps tags to kernel faces internally is
  not documented publicly. Not found.

### Cost to copy

- Low to medium on OCCT: wall-from-segment and cap naming fall straight out of
  `BRepPrimAPI_MakePrism::Generated(edge)` and `FirstShape()/LastShape()` (section 3a).
  Edge-by-adjacent-faces needs a face-adjacency lookup; OCCT has
  `TopExp::MapShapesAndAncestors` (edge -> list of faces).
  https://occt3d.com/dev/doc/refman/html/class_top_exp.html
- Lessons for coalesce (inference): (a) let the agent name things at creation time (segment
  names, `tag_start`, `tag_end`, tags on generated fillet faces); (b) define edges by named
  faces, not by edge ids; (c) the reference object is structured data (sideFaces/endFaces/index),
  which is ideal for an agent API and for error messages.

## 6. Commercial kernels (high level)

### Parasolid

- Every entity in a part (except fins) has a non-zero integer *node-id*, unique within the part,
  "intended to enable the entity to be identified within a transmit file".
  https://ww3.cad.de/foren/ubb/uploads/schulze/XT_Format_April_2008_tcm73-62642.pdf
  (Parasolid XT Format Reference, April 2008, mirrored copy.) Inference: node-ids are a file
  identity, not a regen identity; they say nothing about which new entity "is" an old one after
  a feature is re-run.
- Attributes are the kernel-level persistence tool. Each attribute definition has an action per
  event: rotate, scale, translate, reflect, **split, merge**, transfer, change. Actions:
  `do_nothing`, `delete`, `transform`, `propagate` ("copy attribute onto split-off node"),
  `keep_sub_dominant`, `keep_if_equal` (keep on merge only if both nodes have equal values),
  `combine` (move from deleted node onto survivor on merge). Classes 1-7 are fixed presets,
  e.g. class 1 = propagate on split, keep_equal on merge; class 6 = propagate on split, combine
  on merge. Same source.
- Inference: an application can stamp each face with a (feature id, role) attribute of a class
  that propagates on split and combines on merge, and the kernel then carries the provenance
  through later operations. This is the same information as OCCT history, but attached to the
  entity instead of returned by the algorithm.
- Not verified: per-operation tracking records (`PK_TOPOL_track_r_t`) in the PK interface. I only
  found them in unofficial copies of PK docs (search results pointing to scribd); official PK
  docs are not public. Not found: any public Siemens doc on how NX or Solid Edge resolve
  references on top of these tools.
- Onshape's modeling servers "make use of the venerable geometric modeling kernel Parasolid and
  constraint solver D-Cubed ... which we license from Siemens PLM".
  https://www.onshape.com/en/blog/how-does-onshape-really-work
  Inference: its `deterministicIds` may build on kernel attributes/tracking; not documented. Not found: an Onshape or Siemens
  source that confirms the internals.

### ACIS (Spatial)

- ACIS documentation (doc.spatial.com) requires a customer login. I could not read any page on
  ACIS attributes or naming. Not found (publicly): ACIS split/merge attribute behavior or a
  persistent-naming API description.
  https://doc.spatial.com/get_doc_page/articles/a/n/n/Annotations.html (login page)

### Takeaway (inference)

- Commercial kernels supply *mechanisms* (entity attributes with split/merge rules, or tracking
  records). The *naming policy* (what a reference means and how to resolve it after regen)
  lives in the CAD application above the kernel (Onshape queries, Kripac's Topological ID
  System at Autodesk, etc.). coalesce is in the same position on OCCT: OCCT gives history
  (section 3); coalesce must own the policy.

## 7. Academic work on persistent naming

- **Kripac 1995/1997 (Autodesk), "Topological ID System".** FaceId = [stepId, faceIndex,
  surfaceType] where faceIndex is relative to the creating modeling step. EdgeId = [ordered
  cyclic list of adjacent FaceIds, unordered sets of FaceIds at each end, edge intersection code,
  geometry type]; VertexId = set of adjacent FaceIds + intersection codes. A per-model DAG
  (FaceIdGraph) records how faces were created, split, merged, deleted. Mapping an old ID to the
  new model returns a status: exact, unique (changed but tracked to one), some (mapped to more
  than one: split), failed. A request mode says what the caller accepts (e.g. any vs one); if
  not satisfied, the system rolls the new model back step by step and retries. Split faces are
  matched by similarity of their surrounding faces (FaceIdLoop). The application must "check
  whether exactly one new entity has been obtained. If not ... react appropriately."
  Computer-Aided Design 29(2):113-122, 1997, https://doi.org/10.1016/S0010-4485(96)00040-1 ;
  SMA 1995 https://doi.org/10.1145/218013.218024 ; full text:
  https://quaoar.su/files/papers/misc/Kripac%20-%201997%20-%20A%20mechanism%20for%20persistently%20naming%20topological%20entities%20in%20history-based%20parametric%20solid%20models.pdf
  Note: Zoo's `sideFaces/endFaces/index` edge reference (section 5) has the same shape as
  Kripac's EdgeId (adjacent faces, end faces, disambiguator). Inference: KCL reinvented or
  adopted Kripac's edge naming.
- **Capoyleas, Chen, Hoffmann 1996, "Generic naming in generative, constraint-based design".**
  Replace geometric references in the design history with *generic names* derived from how
  geometry was generated (sketch elements swept into "proto features"), then map them to the
  B-rep at each regen. Three mechanisms, "neither of these mechanisms is complete", combined:
  generative names from sketch elements; topological context (immediate and extended adjacency
  context graph, computed by breadth-first search); orientation (local orientation of edge use
  and vertex normal triple; feature orientation from extrusion direction or revolution spin).
  Merged coincident entities are named from the geometry that existed before the feature. A
  companion paper covers matching. CAD 28(1):17-26, https://doi.org/10.1016/0010-4485(95)00014-3 ;
  full text https://www.cs.purdue.edu/cgvlab/www/resources/papers/Capoyleas-Computer_aided_design-1996-Generic_naming_in_generative.pdf
- **Marcheix & Pierra 2002, "A survey of the persistent naming problem"** (ACM SMA 2002),
  https://doi.org/10.1145/566282.566288 . I did not read the full text; abstract not available
  to me. Not verified beyond title and venue.
- **Bidarra & Bronsvoort 2002/2003; Bidarra, Nyirenda, Bronsvoort 2005.** Claim that naming
  schemes "end up solving only a fraction of the problem" and that the fix is architectural:
  reference *persistent feature faces* in the parametric (feature) domain instead of B-rep
  faces, derive edges from pairs of feature-face names, and when ambiguous (multiple
  intersections) use half-spaces defined by feature references.
  https://doi.org/10.1109/gmap.2002.1027515 ;
  https://graphics.tudelft.nl/~rafa/myPapers/jrnl-bidarra.CAD05.pdf (CAD&A 2(1-4):517-526, 2005)
  Inference: this is the strongest argument for coalesce's "typed feature graph + semantic
  selectors" direction: name by feature role first, B-rep entity second.
- **Wu, Zhang, Zhang 2001**, "A face based mechanism for naming, recording and retrieving
  topological entities", CAD, https://doi.org/10.1016/S0010-4485(00)00099-3 . Not read; title
  only.
- **Farjana & Han 2018, review.** Classifies mechanisms as topology-based, geometry-based or
  combined (also feature-orientation-based, FaceIdGraph-based) and suggests a combined topology +
  geometry approach. Alexandria Engineering Journal, https://doi.org/10.1016/j.aej.2018.01.007
  (abstract via https://api.openalex.org/works/doi:10.1016/j.aej.2018.01.007 ).
- **Cardot, Marcheix, Skapin, Arnould, Belhaouari 2019**, persistent naming via graph
  transformation rules (Jerboa, generalized maps): name elements by precise characterization in
  the rules that create them. CAD&A 16(5):985-1002,
  https://cad-journal.net/files/vol_16/CAD_16(5)_2019_985-1002.pdf
- Newer work (ML-based matching, etc.): not found in this pass.

Common pattern across the literature (inference): (1) name by creation (step/feature +
generator element), (2) name edges/vertices by adjacent named faces, (3) record split/merge
history, (4) disambiguate with orientation/geometry, (5) return a match status and let the
application decide when the match is not unique.

## 8. Comparison table

| System | Primary mechanism | Survives param edit | Survives reorder | Split | Merge | On failure | Cost on OCCT |
|---|---|---|---|---|---|---|---|
| Onshape | Queries on operation Ids + roles + tracking; `deterministicIds` for picks | yes | yes if referenced feature still earlier (inference) | `qCreatedBy` includes split pieces; idtranslations `SPLIT` | merged entity created by all sources | feature error + Repair UI | query AST: low; ID tracking: medium |
| FreeCAD 1.0 | Element map: history-encoded string names from OCCT history | often | "less likely" | modification index in OCCT order (can silently move) | multi-source name | error, suggestion in edit dialog, auto-fix only if confident | high (FreeCAD); medium for a new closed op set |
| OCCT TNaming | OCAF labels + evolution + selector names | yes, if every op registers history correctly | not documented | via history | via history | `Solve()` fails | high; imposes OCAF data model |
| OCCT history only | `Generated/Modified/IsDeleted`, `BRepTools_History` | n/a (within one run) | n/a | `Modified` -> list | `Modified` from many | n/a | low (raw input) |
| build123d / CadQuery | Code selectors re-run each time; per-op history (`Select.NEW`, CadQuery `History`) | if predicate still unique | n/a (code order) | selector may change count silently | same | silent unless single-result accessor | low |
| Zoo KCL | Names from sketch segments + op tags; edges by side/end faces + index | yes (inference) | yes (lexical) | `endFaces` | not documented | not documented | low-medium |
| Parasolid | Node-ids + attributes with split/merge actions | via app policy | via app policy | `propagate` | `keep_if_equal` / `combine` | app-defined | n/a (not OCCT) |
| Kripac | FaceId(step, index, type), EdgeId(adjacent/end faces), FaceIdGraph | yes | partly (rollback) | similarity of surrounding faces | graph node | status exact/unique/some/failed | medium |

Sources: sections 1-7.

## 9. Recommendation for coalesce

Summary: do not try to give every B-rep entity a permanent name. Make the *reference* the
persistent thing: a typed query stored in the feature graph, resolved fresh on every regen
against per-run provenance captured from OCCT history, with an explicit cardinality contract
and loud, structured errors. This is Onshape's model (queries + roles + statuses), fed by
FreeCAD/build123d-style OCCT history capture, with Zoo/Kripac-style edge references and
Bidarra's "reference feature faces, not B-rep faces" principle. All design statements below
are inference built on the cited evidence.

### Layer 0. Stable ids in the feature graph (source of truth)

- Every feature has a stable, agent-visible id (`base_plate`, `pocket_1`), never an index.
  Every sketch entity has a stable id too (`bottom`, `left`), assigned at creation and kept
  across sketch edits. (Onshape `Id`, KCL segment names.)
- Feature parameters hold `Ref` objects (a query AST), never kernel entity handles or face
  numbers. Do not persist `TopoDS` hashes; they are per-process.

### Layer 1. Per-feature provenance from OCCT history (inside one regen)

- Wrap every kernel operation. After it runs, build one `BRepTools_History` for the whole
  feature (merge sub-steps), from: boolean `History()`, `UnifySameDomain.History()`,
  per-algorithm `Generated/Modified/IsDeleted`, and role accessors (`FirstShape/LastShape`).
  build123d `topology/history.py` and CadQuery `_update_history` are working templates.
- From that history, give each output face/edge/vertex a structured *element key*, e.g.

  ```
  ElementKey(feature="pocket_1", role="wall", source=SketchEntity("pocket_sk", "left"))
  ElementKey(feature="base",     role="cap_end")
  ElementKey(feature="fillet_1", role="blend",  source=<key of the input edge>)
  ElementKey(feature="cut_1",    role="intersection", source=(<face key>, <face key>))
  ```

  plus `modified_by: [feature ids]` lineage. Rules: untouched or modified entities inherit the
  input key (append lineage); generated entities get (this feature, role, generator key); split
  pieces share the parent key and are marked `split`; merged entities carry the set of all
  parent keys and are marked `merged`. (FreeCAD passes 1-2; Onshape `qCreatedBy` split/merge
  semantics.)
- Edges and vertices without a direct generator get keys from adjacent face keys
  (Kripac EdgeId; Zoo `sideFaces/endFaces`; FreeCAD forward pass).
- Each feature type declares its role vocabulary in code (extrude: `cap_start`, `cap_end`,
  `side(segment)`; revolve; fillet: `blend(edge)`; chamfer; shell: `inner(face)`, `opening`;
  pocket: `floor`, `wall(segment)`; boolean: `intersection`). Roles are part of the public,
  typed API for the agent.
- Keep keys as typed records, not FreeCAD-style encoded strings.
- Keep `SimplifyResult` / unify-same-domain decisions explicit per feature, because merges
  collapse keys.

### Layer 2. Semantic query language (agent-facing)

- Provenance/role selectors (primary): `faces(created_by="pocket_1")`,
  `face(role="cap_end", of="base_plate")`, `faces(from_segment=("sk1","left"), of="ext1")`,
  `edges(new_in="cut_1")`, `modified_by(...)`.
- Topological selectors: `edges(between=(Q1, Q2))`, `.ending_at(Q3)` (Zoo/Kripac),
  `adjacent_to(Q)`, `of_body(...)`.
- Geometric predicates (secondary, for filtering inside a provenance set):
  `normal(+Z, tol)`, `plane_at(z=10)`, `z_above(10)`, `geom_type=PLANE|CYLINDER`, `area > x`,
  `radius == r`. Extremes (`max_along(+Z)`) are allowed but must raise on ties.
- Set algebra: `and`, `or`, `minus`.
- Discourage, and mark as unstable, any index-based selector (`nth`), like Onshape's
  `unstableIdComponent` and KCL's "index only as last resort".

### Layer 3. Cardinality contract and resolution status (loud errors)

- Every `Ref` carries an expected cardinality: `one`, `one_or_more`, `exactly(n)`, `any`.
  Fillet/chamfer refs default to `one_or_more`; sketch-plane and mate refs default to `one`.
- Resolution returns a status per ref, modeled on Onshape `idtranslations` and Kripac:
  `OK`, `SPLIT` (one key now on several entities), `MERGED`, `AMBIGUOUS` (predicate tie or
  count above contract), `EMPTY` (deleted / consumed, e.g. edge eaten by a fillet), and
  `ORDER` (refers to a feature that is now later in the graph).
- Any status that violates the contract fails the feature with a structured error:
  the ref, the status, each candidate with its element key, lineage and geometric descriptor
  (type, centroid, normal, area), the last-good resolution, and machine-readable repair
  suggestions (e.g. `add .ending_at(face(role="cap_start", of="base"))`, or
  `use blend(fillet_1, <edge>)`). No silent auto-repair by default (FreeCAD: "First, do no
  harm").

### Layer 4. Geometric fingerprint as a guard, not as identity

- After a successful regen, store a small fingerprint per resolved ref (surface type, normal,
  centroid, area/length, bbox). On the next regen, if a ref resolves `OK` but the fingerprint
  moved more than a threshold or changed type, emit a warning (agent-visible), not an error.
- Optional, opt-in tie-break for `SPLIT` under a `one` contract: pick the piece whose fingerprint
  is closest, but only above a confidence margin, and report it. FreeCAD shows that OCCT order is
  not a safe tie-break.

### Layer 5. Agent ergonomics

- Let the agent name things at creation (KCL style): `extrude(..., name="base_plate",
  tags={"top": "cap_end"})`, tags on fillet faces, named sketch segments. A tag is just an alias
  for a role query.
- Provide `explain(ref)` returning the resolution trace (keys, lineage, candidates) and
  `diff_refs(before, after)` after any edit, like Onshape idtranslations.
- Imported geometry (STEP) has no provenance: keys are `imported(feature, index)` and marked
  unstable; only geometric/topological selectors are reliable there.

### What to skip

- OCAF/TNaming: requires the OCAF document model and strict history-loading order; no evidence
  of Python production use; FreeCAD chose its own map instead (section 3c).
- Encoded string names (FreeCAD) and pure geometric selectors as primary identity
  (CadQuery/build123d style).

### Rough cost (inference, low confidence)

- History capture + key assignment for ~10 core ops (sketch extrude/revolve, pocket, boolean,
  fillet, chamfer, shell, pattern, mirror): similar in size to build123d's `history.py` plus
  per-op role tables. The resolver and query AST are ordinary Python. Most of the real effort is
  the test corpus below and per-op edge cases (sweeps, lofts, sewing, seam edges).

## 10. Test cases the prototype must pass

Each test builds a small graph, resolves refs, edits, regenerates, and checks status +
resolved entity (by fingerprint) + error shape.

Parameter change
1. Box extrude height 10 -> 20; `face(role="cap_end", of="base")` stays `OK`, now at z=20.
2. Sketch rectangle width change; `faces(from_segment=("sk","left"))` stays the same wall.
3. Sketch edit that adds segments (rectangle -> rounded rectangle); old segment walls keep keys;
   new arc walls get new keys; refs to old walls `OK`.
4. Hole diameter change; `edges(between=(hole_wall, cap_end))` stays `OK`.

Feature reorder / insertion / deletion
5. Swap two independent features (fillet and an unrelated pocket); all refs `OK`.
6. Move a referenced feature after its referrer; status `ORDER`, clear error.
7. Insert a boss on the top face upstream of a hole that references the top face; top face is
   modified (gains a loop), ref `OK`, hole unchanged.
8. Delete the referenced feature; status `EMPTY`, error names the missing feature.

Split face
9. Slot cut through the top face splits it in two; ref with `one` -> `SPLIT` error listing both
   pieces; same ref with `one_or_more` -> both pieces.
10. Split then un-split (change slot depth to not cut through); ref returns to `OK`.

Merged faces
11. Two coplanar extrusions fused with unify-same-domain on; ref to either source cap resolves
    to the merged face with status `MERGED` (warning) and both keys in the record.
12. Same with unify off; refs resolve to separate coplanar faces.

Fillet / chamfer on referenced edges
13. Edge ref `edges(between=(side("bottom"), cap_end))`, then fillet that edge; a later feature
    referencing the original edge gets `EMPTY` with suggestion `blend(fillet_1, ...)`.
14. Face adjacent to a fillet (`cap_end`) is modified but stays `OK`.
15. Fillet on an edge that a later edit splits (boolean crosses it): fillet ref with
    `one_or_more` fillets both pieces; with `one` -> `SPLIT`; `.ending_at(...)` disambiguates.
16. Chain: fillet face tagged, then chamfer an edge between that fillet face and a cap (Zoo
    "edges involving generated faces").

Geometric selector traps
17. `max_along(+Z)` face; add a boss of equal height -> `AMBIGUOUS` (tie), not a silent pick.
18. Non-planar face with `normal(+Z)` predicate: defined behavior (error or explicit sampling
    point), not centroid surprise (CadQuery warning).
19. Tolerance: a face at z=10.0000001 vs `plane_at(z=10)`.

Patterns, mirrors, seams, special ops
20. Linear pattern count 3 -> 4; `faces(created_by="pattern_1")` grows; instance-specific ref
    keyed by instance id stays on the same instance.
21. Full 360 revolve (no caps, seam edges); seam edge never matches `between` queries silently.
22. Loft / sweep / shell / thicken: cap and side roles exist and survive a parameter change.

Determinism and robustness
23. Regenerate the same graph twice in one process and in two processes; identical keys and
    resolutions.
24. Save/load graph, regenerate, identical resolutions.
25. Golden tests per op against the pinned OCCT version; re-run on OCCT upgrade to catch
    changes in generated/modified classification.
26. Imported STEP body: provenance selectors refuse with a clear error; geometric selectors work.

## 11. Open questions

- Split policy: is `one_or_more` the right default for fillet/chamfer, and should a fingerprint
  tie-break ever be on by default?
- How to key a generated entity whose generator was split (fillet along a split edge gives two
  blend faces with the same key).
- Unify-same-domain: run it by default (cleaner topology, more `MERGED` keys) or not?
- Sketch entity identity when the agent rewrites a sketch wholesale: diff by name only, or also
  match by geometry?
- Cross-body and multi-part references; what "of=body" means after a boolean combines bodies.
- Do OCP bindings expose every history accessor needed per op (e.g. `FirstShape(S)` on all
  sweeps, `ThruSections` generated faces)? build123d and CadQuery cover the common ones;
  others are unverified.
- Cost of history capture on large models (OCCT lets you disable it, which suggests a cost);
  not measured.
- Should a query be evaluated at its own point in the feature list (Onshape) or against the
  final model? Recommendation assumes its own point.
- How Onshape maps `deterministicIds` across regens and how Zoo resolves face tags on split
  faces: not public (not found).

## 12. Things I could not verify

- Onshape internal ID mapping algorithm (not public).
- Parasolid PK tracking records (only unofficial copies found); ACIS naming/attribute docs
  (login required).
- Full text of Marcheix & Pierra 2002 and Wu et al. 2001 (title/venue only).
- How Zoo's engine maps tags to kernel faces; behavior of a face tag when the face splits.
- FreeCAD 1.0 behavior beyond its wiki and realthunder's wiki (I did not run FreeCAD).
- Nothing here was run against OCCT; all OCCT behavior is from docs and from build123d,
  CadQuery and FreeCAD source.
