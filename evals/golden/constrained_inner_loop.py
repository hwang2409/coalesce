from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from .common import run, score_artifact
from .constrained_common import extrude, plate_model, sketch_create

if __name__ == "__main__":
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    (out / "steps").mkdir()
    document = out / "model.coalesce.json"
    results = asyncio.run(run(document, [sketch_create(plate_model(4)), extrude()]))
    score_artifact(out, "constrained-inner-loop", document, results[-1]["revision"])
