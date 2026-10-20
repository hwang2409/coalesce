"""read-only importers for external CAD formats."""

from .kicad import Board, parse_kicad_pcb

__all__ = ["Board", "parse_kicad_pcb"]
