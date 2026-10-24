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
    (rejected,) = asyncio.run(
        run(document, [sketch_update(plate_model(20), "golden-invalid-update")], require_ok=False)
    )
    if rejected.get("ok") is not False or rejected.get("revision") != 2:
        raise RuntimeError(f"invalid constrained update did not fail atomically: {rejected}")
    (repaired,) = asyncio.run(
        run(document, [sketch_update(plate_model(4), "golden-valid-repair")])
    )
    score_artifact(out, "failed-solve-repair", document, repaired["revision"])
