from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from .common import run, score_artifact
from .constrained_common import extrude, plate_model, sketch_create, sketch_update

if __name__ == "__main__":
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    (out / "steps").mkdir()
    document = out / "model.coalesce.json"
    asyncio.run(run(document, [sketch_create(plate_model(3)), extrude()]))
    (updated,) = asyncio.run(
        run(document, [sketch_update(plate_model(4), "golden-valid-update")])
    )
    score_artifact(
        out, "constrained-sketch-update-after-extrusion", document, updated["revision"]
    )
