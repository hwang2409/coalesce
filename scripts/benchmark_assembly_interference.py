"""Benchmark the 10-second assembly interference path outside default CI."""

import copy
import json
import math
import statistics
from time import monotonic

from build123d import (
    Axis,
    Box,
    BuildLine,
    BuildSketch,
    Spline,
    Vector,
    extrude,
    fillet,
    insert,
    make_face,
)

from coalesce import assembly_inspection
from coalesce.assembly import Occurrence, ResolvedOccurrence, RigidTransform, locate_shape
from coalesce.parts import TerminalPart
from coalesce.references import BodySource


def cycloidal_disc():
    points = []
    for index in range(280):
        angle = 2 * math.pi * index / 280
        correction = math.atan2(
            math.sin(-7 * angle),
            30 / 16 - math.cos(-7 * angle),
        )
        points.append(
            Vector(
                30 * math.cos(angle)
                - 4 * math.cos(angle + correction)
                - 2 * math.cos(8 * angle),
                -30 * math.sin(angle)
                + 4 * math.sin(angle + correction)
                + 2 * math.sin(8 * angle),
            )
        )
    with BuildLine() as outline:
        Spline(*points, periodic=True)
    with BuildSketch() as sketch:
        insert(outline.line)
        make_face()
    return extrude(sketch.sketch, amount=5)


def resolved_occurrence(index, shape, part_id):
    transform = RigidTransform(
        ((index % 4) * 10, (index // 4) * 8, 0),
        (0, 0, 1),
        (index * 11) % 180,
    )
    occurrence = Occurrence(f"occurrence-{index:02}", part_id, transform)
    owned_shape = copy.deepcopy(shape)
    part = TerminalPart(
        part_id,
        part_id,
        "manufacturing",
        BodySource(part_id, "body"),
        part_id,
        owned_shape,
    )
    return ResolvedOccurrence(occurrence, part, locate_shape(owned_shape, transform))


def main():
    disc = cycloidal_disc()
    block = fillet(Box(70, 50, 18).edges().filter_by(Axis.Z), radius=3)
    occurrences = [
        resolved_occurrence(
            index,
            disc if index % 2 == 0 else block,
            "cycloidal-disc" if index % 2 == 0 else "filleted-part",
        )
        for index in range(16)
    ]
    occurrence_ids = {id(item.shape): item.occurrence.id for item in occurrences}
    common_timings = []
    common_volume = assembly_inspection._common_volume

    def timed_common(first, second, symbols):
        started = monotonic()
        try:
            return common_volume(first, second, symbols)
        finally:
            common_timings.append(
                {
                    "occurrenceIds": [occurrence_ids[id(first)], occurrence_ids[id(second)]],
                    "seconds": monotonic() - started,
                }
            )

    assembly_inspection._common_volume = timed_common
    started = monotonic()
    rows = assembly_inspection.inspect_assembly(
        occurrences,
        None,
        1,
        measure_clearance=False,
    )
    total_seconds = monotonic() - started
    completed_pairs = sum(row.get("error", {}).get("reason") != "DEADLINE" for row in rows)
    durations = [timing["seconds"] for timing in common_timings]
    print(
        json.dumps(
            {
                "totalSeconds": total_seconds,
                "pairsFinished": completed_pairs,
                "pairCount": len(rows),
                "commonCalls": len(common_timings),
                "commonSeconds": {
                    "mean": statistics.mean(durations),
                    "median": statistics.median(durations),
                    "minimum": min(durations),
                    "maximum": max(durations),
                },
                "perPairCommonSeconds": common_timings,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
