import copy
from dataclasses import replace

import pytest
from build123d import Box, Compound, Shell
from OCP.TopAbs import TopAbs_ON, TopAbs_UNKNOWN

from coalesce.assembly import Occurrence, ResolvedOccurrence, RigidTransform, locate_shape
from coalesce.assembly_inspection import (
    _bounding_box,
    _boxes_overlap,
    _common_volume,
    _load_kernel_symbols,
    _minimum_clearance,
    inspect_assembly,
)
from coalesce.parts import TerminalPart
from coalesce.references import BodySource


def _resolved(
    occurrence_id,
    *,
    translation=(0, 0, 0),
    angle=0,
    part_id="bar/body",
    shape=None,
):
    shape = Box(10, 2, 2) if shape is None else shape
    transform = RigidTransform(translation, (0, 0, 1), angle)
    occurrence = Occurrence(occurrence_id, part_id, transform)
    part = TerminalPart(part_id, "bar", "manufacturing", BodySource("bar", "body"), "bar", shape)
    return ResolvedOccurrence(occurrence, part, locate_shape(shape, transform))


def test_common_is_configured_before_building_snapshot_shapes():
    first = copy.deepcopy(Box(10, 10, 10))
    second = copy.deepcopy(Box(10, 10, 10))
    symbols = _load_kernel_symbols(False)
    common = symbols.common
    build_state = []

    class CommonSpy:
        def __init__(self):
            self.operation = common()
            self.non_destructive = False
            self.parallel = False

        def __getattr__(self, name):
            return getattr(self.operation, name)

        def SetNonDestructive(self, enabled):
            self.non_destructive = enabled
            self.operation.SetNonDestructive(enabled)

        def SetRunParallel(self, enabled):
            self.parallel = enabled
            self.operation.SetRunParallel(enabled)

        def Build(self):
            build_state.append((self.non_destructive, self.parallel, self.operation.IsDone()))
            self.operation.Build()

    assert _common_volume(first, second, replace(symbols, common=CommonSpy)) == pytest.approx(1000)
    assert build_state == [(True, True, False)]


def test_bounds_are_built_once_per_occurrence(monkeypatch):
    symbols = _load_kernel_symbols(False)
    add_bounds = symbols.add_bounds
    calls = []

    def count_bounds(*args):
        calls.append(args[0])
        return add_bounds(*args)

    monkeypatch.setattr(
        "coalesce.assembly_inspection._load_kernel_symbols",
        lambda _measure_clearance: replace(symbols, add_bounds=count_bounds),
    )

    inspect_assembly(
        [
            _resolved("a"),
            _resolved("b", translation=(20, 0, 0)),
            _resolved("c", translation=(40, 0, 0)),
        ],
        None,
        1,
        measure_clearance=False,
    )

    assert len(calls) == 3


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
    assert _boxes_overlap(_bounding_box(apart[0].shape), _bounding_box(apart[1].shape)) is True
    false_positive = inspect_assembly(apart, None, 9)[0]
    assert false_positive["overlapVolume"] == {"value": 0.0, "unit": "mm^3"}
    assert false_positive["clearance"]["value"] == pytest.approx(0.3639610307)
    assert false_positive["interferes"] is False


def test_separated_pair_with_clearance_skips_common(monkeypatch):
    def fail(*_args):
        raise AssertionError("Common must not run after exact separation")

    apart = [_resolved("a", angle=45), _resolved("b", translation=(2, 7, 0), angle=-45)]
    assert _boxes_overlap(_bounding_box(apart[0].shape), _bounding_box(apart[1].shape)) is True
    monkeypatch.setattr("coalesce.assembly_inspection._common_volume", fail)

    row = inspect_assembly(apart, None, 9)[0]

    assert row["overlapVolume"] == {"value": 0.0, "unit": "mm^3"}
    assert row["clearance"]["value"] == pytest.approx(0.3639610307)
    assert row["interferes"] is False


def test_touching_pair_with_clearance_still_calls_common(monkeypatch):
    calls = 0
    common_volume = _common_volume

    def count_common(*args):
        nonlocal calls
        calls += 1
        return common_volume(*args)

    monkeypatch.setattr("coalesce.assembly_inspection._common_volume", count_common)

    row = inspect_assembly([_resolved("a"), _resolved("b", translation=(10, 0, 0))], None, 1)[0]

    assert calls == 1
    assert row["overlapVolume"] == {"value": 0.0, "unit": "mm^3"}
    assert row["clearance"] == {"value": 0.0, "unit": "mm"}


def test_overlapping_pair_keeps_exact_volume():
    row = inspect_assembly([_resolved("a"), _resolved("b")], None, 1)[0]

    assert row["overlapVolume"]["value"] == pytest.approx(40.0)
    assert row["clearance"] == {"value": 0.0, "unit": "mm"}


@pytest.mark.parametrize("small_id", ["a", "b"])
def test_contained_box_keeps_exact_volume_in_both_orders(small_id):
    large_id = "b" if small_id == "a" else "a"
    shapes = {small_id: Box(2, 2, 2), large_id: Box(10, 10, 10)}
    symbols = _load_kernel_symbols(True)
    assert _minimum_clearance(shapes[small_id], shapes[large_id], symbols) > symbols.linear_tolerance

    row = inspect_assembly(
        [_resolved(occurrence_id, shape=shapes[occurrence_id]) for occurrence_id in ("a", "b")],
        None,
        1,
    )[0]

    assert row["overlapVolume"]["value"] == pytest.approx(8.0)
    assert row["clearance"] == {"value": 0.0, "unit": "mm"}
    assert row["interferes"] is True


def test_contained_fillet_keeps_exact_volume():
    small = Box(4, 4, 2)
    small = small.fillet(0.4, small.edges())
    large = Box(10, 10, 10)
    symbols = _load_kernel_symbols(True)
    assert _minimum_clearance(small, large, symbols) > symbols.linear_tolerance

    row = inspect_assembly(
        [_resolved("a", shape=small), _resolved("b", shape=large)],
        None,
        1,
    )[0]

    assert row["overlapVolume"]["value"] == pytest.approx(small.volume)
    assert row["clearance"] == {"value": 0.0, "unit": "mm"}
    assert row["interferes"] is True


def test_multi_solid_containment_keeps_exact_volume():
    inside = Box(2, 2, 2)
    compound = Compound(children=[Box(2, 2, 2).translate((20, 0, 0)), inside])
    large = Box(10, 10, 10)
    symbols = _load_kernel_symbols(True)
    assert _minimum_clearance(compound, large, symbols) > symbols.linear_tolerance

    row = inspect_assembly(
        [_resolved("a", shape=compound), _resolved("b", shape=large)],
        None,
        1,
    )[0]

    assert row["overlapVolume"]["value"] == pytest.approx(inside.volume)
    assert row["clearance"] == {"value": 0.0, "unit": "mm"}
    assert row["interferes"] is True


def test_open_shell_cannot_prove_separation(monkeypatch):
    shell = Shell(Box(10, 10, 10).faces()[:-1])
    inside = Box(2, 2, 2)
    symbols = _load_kernel_symbols(True)
    calls = 0
    common_volume = _common_volume
    assert _minimum_clearance(shell, inside, symbols) > symbols.linear_tolerance

    def count_common(*args):
        nonlocal calls
        calls += 1
        return common_volume(*args)

    monkeypatch.setattr("coalesce.assembly_inspection._common_volume", count_common)
    inspect_assembly([_resolved("a", shape=shell), _resolved("b", shape=inside)], None, 1)

    assert calls == 1


@pytest.mark.parametrize("state", [TopAbs_ON, TopAbs_UNKNOWN, RuntimeError("classifier failed")])
def test_uncertain_classifier_state_cannot_prove_separation(monkeypatch, state):
    symbols = _load_kernel_symbols(True)
    calls = 0
    common_volume = _common_volume

    class Classifier:
        def __init__(self, *_args):
            if isinstance(state, BaseException):
                raise state

        def State(self):
            return state

    def count_common(*args):
        nonlocal calls
        calls += 1
        return common_volume(*args)

    monkeypatch.setattr(
        "coalesce.assembly_inspection._load_kernel_symbols",
        lambda _measure_clearance: replace(symbols, classifier=Classifier),
    )
    monkeypatch.setattr("coalesce.assembly_inspection._common_volume", count_common)
    apart = [_resolved("a", angle=45), _resolved("b", translation=(2, 7, 0), angle=-45)]

    inspect_assembly(apart, None, 1)

    assert calls == 1


def test_overlapping_pair_omits_clearance_when_not_requested():
    row = inspect_assembly(
        [_resolved("a"), _resolved("b")], None, 1, measure_clearance=False
    )[0]

    assert row["interferes"] is True
    assert "clearance" not in row


def test_clearance_within_kernel_tolerance_still_calls_common(monkeypatch):
    symbols = _load_kernel_symbols(True)
    calls = 0

    def count_common(*_args):
        nonlocal calls
        calls += 1
        return 0.0

    monkeypatch.setattr(
        "coalesce.assembly_inspection._minimum_clearance",
        lambda *_args: symbols.linear_tolerance / 2,
    )
    monkeypatch.setattr("coalesce.assembly_inspection._common_volume", count_common)

    row = inspect_assembly([_resolved("a"), _resolved("b")], None, 1)[0]

    assert calls == 1
    assert row["clearance"]["value"] == symbols.linear_tolerance / 2
    assert row["interferes"] is False


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
