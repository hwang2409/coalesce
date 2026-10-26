this example builds a seven-lobe cycloidal disc, a revolved stepped shaft, one 4 mm radius pin definition, and a stationary ring through public stdio mcp calls. the ring has a named centre bore and an eight-member circular hole pattern (`bolt1` through `bolt8`) on a 30 mm radius.

the assembly uses geometry-backed intent instead of calculated pin placements:

- the ring is grounded at z = -2 mm;
- the shaft cylinder is concentric with the ring centre bore;
- each pin cylinder is concentric with its corresponding named ring hole and seated on the ring face;
- a journal occurrence is grounded 2 mm from the drive axis;
- the disc bore is concentric with that journal, so the solve produces the disc's 2 mm eccentric transform.

v1 cannot seat the spline-extruded disc cap because occt reports that cap as a bspline surface, not a plane. the disc therefore keeps its seeded axial position and spin as the two free motions of its concentric mate. this stays inside the supported closed-form topology and does not widen the solver.

run the reproducible workflow without browser capture:

```bash
uv run --extra sketch python examples/cycloidal_drive_assembly_demo.py --out /tmp/cycloidal-drive-assembly
```

add `--screenshots` to capture a desktop assembled view, a desktop pin-mate focus, and a phone view when a launchable playwright chromium is available. the helper always closes each page and the browser.

the output contains the revisioned document, `transcript.json`, and `cycloidal-drive-assembly.step`. the transcript records:

- the initial mate graph and solved transforms;
- a pattern-radius edit from 30 mm to 31 mm, which moves all eight pin occurrences without changing occurrence or mate ids;
- an atomic `MATE_REFERENCE_STALE` rejection when an edit would remove `bolt8`;
- restoration to 30 mm before exact disc-to-pin meshing checks and export;
- a byte-stable restart query and fresh-process xcaf evidence.

changing only the pattern radius to 31 mm moves the pins radially outward. identities and mate intent stay stable, but the eight-member mesh with the unchanged eccentric disc is lost: exact checks show zero overlap, seven pins gain positive clearance, and one remains tangent within kernel tolerance. the demo reports this edited state, then restores 30 mm before it proves all eight pins have zero overlap and at most kernel-tolerance clearance to the disc.

## viewer inspection

the viewer lists the assembly solve state, under-constrained occurrence ids, grounded count, conflict count, and every mate. occurrence rows say `explicit`, `grounded`, or `mated` and show remaining degrees of freedom. a mate row shows its id, kind, occurrences, solve status, orientation, and planar offset when present. selecting a mate focuses both occurrences through the existing selection system. rejected conflicts continue to use the viewer's committed-revision failure banner.

## export contract

use `mode: "assembly"` for a continuation-ready step product tree. assembly mode exports every manufacturing occurrence and does not accept `partIds`. it creates one product definition for each unique manufacturing `partId` and one named component for each occurrence. reference occurrences stay in the coalesce document and appear in `excludedOccurrenceIds`.

`action: "preflight"` builds and verifies the same normalized bytes without publishing a path. publication validates first, stages a private sibling file, and uses one exclusive rename as the commit point. a path collision, write failure, or validation mismatch publishes nothing.

fresh-process validation reads the product tree through `STEPCAFControl_Reader`. it checks one root, four referred definitions, 12 named occurrences, placements, solid validity, bounding box, volume, and area. a flattened geometry reimport is not accepted as proof of assembly structure.
