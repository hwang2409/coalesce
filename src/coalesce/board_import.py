"""Trusted local-file seam for persisted KiCad board snapshots."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from .importers.kicad import Board, parse_kicad_pcb_bytes

MAX_KICAD_IMPORT_BYTES = 16 * 1024 * 1024


class BoardImportError(ValueError):
    pass


def read_kicad_board(path: str) -> tuple[Path, Board]:
    """Resolve and read one regular local file, then parse the captured bytes."""
    requested = Path(path)
    if not requested.is_absolute():
        raise BoardImportError("KiCad import path must be absolute")
    try:
        resolved = requested.resolve(strict=True)
        with resolved.open("rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise BoardImportError("KiCad import path must name a regular file")
            raw = stream.read(MAX_KICAD_IMPORT_BYTES + 1)
    except BoardImportError:
        raise
    except OSError as exc:
        raise BoardImportError(f"KiCad source could not be read: {exc}") from None
    if len(raw) > MAX_KICAD_IMPORT_BYTES:
        raise BoardImportError(
            f"KiCad source exceeds the {MAX_KICAD_IMPORT_BYTES}-byte limit"
        )
    board = parse_kicad_pcb_bytes(raw)
    if board.errors:
        kinds = ", ".join(sorted({error.kind for error in board.errors}))
        raise BoardImportError(f"KiCad source has invalid mechanics ({kinds})")
    return resolved, board
