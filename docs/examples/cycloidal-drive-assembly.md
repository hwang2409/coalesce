# cycloidal drive assembly

this example builds a seven-lobe cycloidal disc, a revolved stepped shaft, and one pin definition through public stdio mcp calls. it then places the disc at a 2 mm eccentric offset, rotates the shaft 90 degrees about y, and creates eight named pin occurrences on a 30 mm circle.

run the reproducible workflow without browser capture:

```bash
uv run --extra sketch python examples/cycloidal_drive_assembly_demo.py --out /tmp/cycloidal-drive-assembly
```

add `--screenshots` to capture desktop and phone views when a launchable playwright chromium is available. the helper always closes each page and the browser.

the output contains the revisioned document, `transcript.json`, and `cycloidal-drive-assembly.step`. the transcript records exact brep and assembly-interference checks, the assembly query before restart, a matching query after restart, and fresh-process xcaf evidence.

## export contract

use `mode: "assembly"` for a continuation-ready step product tree. assembly mode exports every manufacturing occurrence and does not accept `partIds`. it creates one product definition for each unique manufacturing `partId` and one named component for each occurrence. reference occurrences stay in the coalesce document and appear in `excludedOccurrenceIds`.

`action: "preflight"` builds and verifies the same normalized bytes without publishing a path. publication validates first, stages a private sibling file, and uses one exclusive rename as the commit point. a path collision, write failure, or validation mismatch publishes nothing.

fresh-process validation reads the product tree through `STEPCAFControl_Reader`. it checks the root, referred definition labels, occurrence names, placements, solid validity, bounding box, volume, and area. a flattened geometry reimport is not accepted as proof of assembly structure.
