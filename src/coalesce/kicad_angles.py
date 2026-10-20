"""KiCad rotation conversion shared by import and regenerated geometry."""


def kicad_angle(degrees: float) -> float:
    """Convert KiCad clockwise degrees to normalized counterclockwise degrees."""
    return (-degrees) % 360.0
