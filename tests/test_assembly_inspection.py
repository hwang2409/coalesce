import copy
from dataclasses import replace

import pytest
from build123d import Box

from coalesce.assembly import Occurrence, ResolvedOccurrence, RigidTransform, locate_shape
from coalesce.assembly_inspection import (
    _boxes_overlap,
    _common_volume,
    _load_kernel_symbols,
    inspect_assembly,
)
from coalesce.parts import TerminalPart
from coalesce.references import BodySource


def _resolved(occurrence_id, *, translation=(0, 0, 0), angle=0, part_id="bar/body"):
    shape = Box(10, 2, 2)
    transform = RigidTransform(translation, (0, 0, 1), angle)
    occurrence = Occurrence(occurrence_id, part_id, transform)
    part = TerminalPart(part_id, "bar", "manufacturing", BodySource("bar", "body"), "bar", shape)
    return ResolvedOccurrence(occurrence, part, locate_shape(shape, transform))


def test_common_is_configured_before_building_snapshot_shapes():
    first = copy.deepcopy(Box(10, 10, 10))
    second = copy.deepcopy(Box(10, 10, 10))
    symbols = _load_kernel_symbols(False)
    common = symbols.common
    initial_state = []

    def capture_initial_state(*args):
        operation = common(*args)
        initial_state.append((len(args), operation.IsDone()))
        return operation

    assert _common_volume(
        first, second, replace(symbols, common=capture_initial_state)
    ) == pytest.approx(1000)
    assert initial_state == [(0, False)]


def test_exact_pairs_cover_rotated_overlap_touch_clearance_and_aabb_false_positive():
    overlap = inspect_assembly([_resolved("a", angle=45), _resolved("b", angle=-45)], None, 7)[0]
    assert overlap["occurrenceIds"] == ["a", "b"]
    assert overlap["partIds"] == ["bar/body", "bar/body"]
    assert overlap["overlapVolume"]["unit"] == "mm^3"
    assert overlap["overlapVolume"]["value"] > 0
    assert overlap["clearance"] == {"value": 0.0, "unit": "mm"}
    assert overlap["interferes"] is True
    assert overlap["revision"] == 7

    touching = inspect_assembly([_resolved("a"), _resolved("b", translation=(10, 0, 0))], None, 8)[0]
    assert touching["overlapVolume"] == {"value": 0.0, "unit": "mm^3"}
    assert touching["clearance"] == {"value": 0.0, "unit": "mm"}
    assert touching["interferes"] is False

    apart = [_resolved("a", angle=45), _resolved("b", translation=(2, 7, 0), angle=-45)]
    assert _boxes_overlap(apart[0].shape, apart[1].shape) is True
    false_positive = inspect_assembly(apart, None, 9)[0]
    assert false_positive["overlapVolume"] == {"value": 0.0, "unit": "mm^3"}
    assert false_positive["clearance"]["value"] == pytest.approx(0.3639610307)
    assert false_positive["interferes"] is False


def test_repeated_definition_occurrences_remain_distinct_stable_pairs():
    rows = inspect_assembly(
        [_resolved("c", translation=(35, 0, 0)), _resolved("a"), _resolved("b", translation=(22, 0, 0))],
        None,
        3,
    )
    assert [row["occurrenceIds"] for row in rows] == [["a", "b"], ["a", "c"], ["b", "c"]]
    assert all(row["partIds"] == ["bar/body", "bar/body"] for row in rows)
    assert all(row["clearance"]["value"] > 0 for row in rows)


def test_clearance_is_skipped_when_not_requested(monkeypatch):
    calls = 0

    def count(*_args):
        nonlocal calls
        calls += 1
        return 1.0

    monkeypatch.setattr("coalesce.assembly_inspection._minimum_clearance", count)
    rows = inspect_assembly(
        [_resolved("a"), _resolved("b", translation=(20, 0, 0))],
        None,
        1,
        measure_clearance=False,
    )

    assert calls == 0
    assert "clearance" not in rows[0]


@pytest.mark.parametrize("operation", ["_common_volume", "_minimum_clearance"])
def test_kernel_measurement_failure_is_error_not_pass(monkeypatch, operation):
    def fail(*_args):
        raise ValueError("forced exact measurement failure")

    monkeypatch.setattr(f"coalesce.assembly_inspection.{operation}", fail)
    result = inspect_assembly([_resolved("a"), _resolved("b", translation=(10, 0, 0))], None, 1)[0]
    assert result["error"]["code"] == "INSPECTION_FAILED"
    assert "forced exact measurement failure" in result["error"]["message"]
    assert "interferes" not in result
