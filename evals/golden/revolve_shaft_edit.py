from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from evals.run import build_before_state

from .common import run, score_artifact
from .revolve_shaft_common import profile_update

if __name__ == "__main__":
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    (out / "steps").mkdir()
    fixtures = out / "fixtures"
    fixtures.mkdir()
    document = out / "model.coalesce.json"
    before_state = out / "before-state.json"
    build_before_state("revolve-shaft-edit", document, fixtures, before_state)
    updated, checked = asyncio.run(run(document, [
        profile_update(),
        (
            "check",
            {
                "action": "evaluate",
                "revision": 4,
                "requirements": [{"id": "valid", "kind": "valid_brep"}],
            },
        ),
    ]))
    if checked["results"][0]["status"] != "PASS":
        raise RuntimeError(f"golden validity check failed: {checked}")
    score_artifact(
        out,
        "revolve-shaft-edit",
        document,
        updated["revision"],
        before_state,
    )
