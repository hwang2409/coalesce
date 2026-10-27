this example builds a seven-lobe cycloidal disc, one shaft solid with a joined eccentric journal, one 4 mm radius pin definition, and a stationary ring through public stdio mcp calls. the ring has a named centre bore and an eight-member circular hole pattern (`bolt1` through `bolt8`) on a 30 mm radius.

the assembly uses geometry-backed intent instead of calculated pin placements:

- the ring is grounded at z = -2 mm;
- the shaft is concentric with the ring centre bore and seated on the ring face;
- each pin is concentric with its corresponding named ring hole and seated on the ring face;
- the shaft's joined journal is offset 2 mm from the drive axis;
- the disc bore is concentric with the journal, and the planar spline cap is seated on the shaft shoulder.

the result is a static pose of the mechanism. the mate solve is static, not simulated motion. the seeded shaft spin is preserved at 0 degrees, so the eccentric journal points along +x and the solve places the disc at x = 2 mm. the shaft, disc, and pins each retain only spin; the demo does not animate coupled cycloidal motion.

run the reproducible workflow without browser capture:

```bash
coalesce-demo --output ./cycloidal-drive-assembly-demo
```

add `--screenshots` to capture a desktop assembled view, a desktop pin-mate focus, and a phone view when a launchable playwright chromium is available. the helper always closes each page and the browser.

the output contains the revisioned document, `transcript.json`, and `cycloidal-drive-assembly.step`. the transcript records:

- 11 manufacturing occurrences, 4 definitions, 21 mates, and solved transforms;
- a pattern-radius edit from 30 mm to 31 mm, which moves all eight pin occurrences without changing occurrence or mate ids;
- an atomic `MATE_REFERENCE_STALE` rejection when an edit would remove `bolt8`;
- restoration to 30 mm before exact whole-assembly interference checks and export;
- a byte-stable restart query and fresh-process xcaf evidence.

changing only the pattern radius to 31 mm moves the pins radially outward. identities and mate intent stay stable, but the eight-member mesh with the unchanged eccentric disc is lost: exact checks show zero overlap, six pins gain positive clearance, and two remain tangent within kernel tolerance. the demo reports this edited state, then restores 30 mm.

at the restored radius, one all-pairs interference requirement covers all 55 pairs of exported manufacturing occurrences. expected contacts are ring-to-shaft, ring-to-each-pin, disc-to-shaft, and disc-to-each-pin. every expected contact has zero overlap and at most kernel-tolerance clearance; every other pair has positive clearance. no exported occurrence is omitted from the evidence.

## viewer inspection

the viewer presents this valid mechanism as `solved · free motion remains`, then groups the remaining motions by part, such as `8 pins: spin`. warning colour is reserved for solve conflicts or remaining translation. occurrence rows still say `explicit`, `grounded`, or `mated` and show remaining degrees of freedom. a mate row shows its id, kind, occurrences, solve status, orientation, and planar offset when present. selecting a mate focuses both occurrences through the existing selection system. rejected conflicts continue to use the viewer's committed-revision failure banner.

## export contract

use `mode: "assembly"` for a continuation-ready step product tree. assembly mode exports every manufacturing occurrence and does not accept `partIds`. it creates one product definition for each unique manufacturing `partId` and one named component for each occurrence. reference occurrences stay in the coalesce document and appear in `excludedOccurrenceIds`.

`action: "preflight"` builds and verifies the same normalized bytes without publishing a path. publication validates first, stages a private sibling file, and uses one exclusive rename as the commit point. a path collision, write failure, or validation mismatch publishes nothing.

fresh-process validation reads the product tree through `STEPCAFControl_Reader`. it checks one root, four referred definitions, 11 named occurrences, placements, solid validity, bounding box, volume, and area. a flattened geometry reimport is not accepted as proof of assembly structure.
