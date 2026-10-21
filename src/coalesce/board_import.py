"""Trusted local-file seam for persisted KiCad board snapshots."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from .graph import BoardImport
from .importers.kicad import Board, parse_kicad_pcb_bytes

MAX_KICAD_IMPORT_BYTES = 16 * 1024 * 1024


class BoardImportError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def mounting_holes(board: BoardImport) -> list[dict]:
    """Classify board holes and return those from MountingHole:* footprints."""
    footprints = {
        footprint["id"]: footprint
        for footprint in board.mechanics.get("footprints", [])
    }
    mounting = []
    for hole in board.mechanics.get("holes", []):
        footprint = footprints.get(hole.get("footprintId"), {})
        reference = footprint.get("reference")
        hole["reference"] = None if reference in (None, "REF**") else reference
        hole["mounting"] = footprint.get("libraryId", "").startswith("MountingHole:")
        if hole["mounting"]:
            mounting.append(hole)
    return mounting


def read_kicad_board(path: str) -> tuple[Path, Board]:
    """Resolve and read one regular local file, then parse the captured bytes."""
    requested = Path(path)
    if not requested.is_absolute():
        raise BoardImportError(
            "IMPORT_PATH_NOT_ABSOLUTE", "KiCad import path must be absolute"
        )
    try:
        resolved = requested.resolve(strict=True)
        flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(resolved, flags)
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise BoardImportError(
                    "IMPORT_PATH_NOT_FILE", "KiCad import path must name a regular file"
                )
            raw = stream.read(MAX_KICAD_IMPORT_BYTES + 1)
    except BoardImportError:
        raise
    except OSError as exc:
        raise BoardImportError(
            "IMPORT_SOURCE_UNREADABLE", f"KiCad source could not be read: {exc}"
        ) from None
    if len(raw) > MAX_KICAD_IMPORT_BYTES:
        raise BoardImportError(
            "IMPORT_TOO_LARGE",
            f"KiCad source exceeds the {MAX_KICAD_IMPORT_BYTES}-byte limit",
        )
    board = parse_kicad_pcb_bytes(raw)
    if board.errors:
        kinds = ", ".join(sorted({error.kind for error in board.errors}))
        raise BoardImportError(
            "IMPORT_INVALID_MECHANICS", f"KiCad source has invalid mechanics ({kinds})"
        )
    return resolved, board
