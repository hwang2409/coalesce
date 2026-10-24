from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path

from evals.run import build_before_state

from .common import FIXTURES, q, run, score_artifact


def pattern_update(*, count=4, pitch=5):
    return (
        "feature",
        {
            "action": "update",
            "expectedRevision": 6,
            "requestId": "golden-pattern-edit",
            "featureId": "lid_pattern",
            "kind": "hole",
            "set": {"pattern": {"kind": "linear", "pitch": q(pitch), "count": count}},
        },
    )


if __name__ == "__main__":
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    (out / "steps").mkdir()
    fixtures = out / "fixtures"
    fixtures.mkdir()
    shutil.copyfile(FIXTURES / "features.kicad_pcb", fixtures / "features.kicad_pcb")
    document = out / "model.coalesce.json"
    before_state = out / "before-state.json"
    build_before_state("screw-closure-pattern-edit", document, fixtures, before_state)
    (updated,) = asyncio.run(run(document, [pattern_update()]))
    revision = updated["revision"]
    (valid,) = asyncio.run(run(document, [
        (
            "check",
            {
                "action": "evaluate",
                "revision": revision,
                "requirements": [{"id": "valid", "kind": "valid_brep"}],
            },
        )
    ]))
    if valid["results"][0]["status"] != "PASS":
        raise RuntimeError(valid)
    score_artifact(out, "screw-closure-pattern-edit", document, revision, before_state)
