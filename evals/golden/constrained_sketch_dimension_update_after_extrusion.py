import asyncio
import sys
from pathlib import Path

from evals.run import build_before_state

from .common import run, score_artifact

if __name__ == "__main__":
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    (out / "steps").mkdir()
    fixtures = out / "fixtures"
    fixtures.mkdir()
    document = out / "model.coalesce.json"
    before_state = out / "before-state.json"
    build_before_state(
        "constrained-sketch-update-after-extrusion", document, fixtures, before_state
    )
    payload = {
        "action": "update",
        "expectedRevision": 2,
        "requestId": "golden-dimension-update",
        "sketchId": "profile",
        "set": {"dimensionValues": {"opening-radius": 4}},
    }
    (updated,) = asyncio.run(run(document, [("sketch", payload)]))
    score_artifact(
        out,
        "constrained-sketch-update-after-extrusion",
        document,
        updated["revision"],
        before_state,
    )
