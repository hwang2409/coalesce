from __future__ import annotations

import sys
from pathlib import Path

from .common import build_calls, finish_artifact

if __name__ == "__main__":
    finish_artifact(Path(sys.argv[1]), "pcb-edge-treatments", build_calls(Path(sys.argv[2]) if len(sys.argv)>2 else Path("tests/fixtures/kicad/features.kicad_pcb")))
