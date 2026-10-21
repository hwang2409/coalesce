from __future__ import annotations

import ast
import builtins
import math
import os
import random
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

import pytest

if os.environ.get("COALESCE_REQUIRE_SKETCH"):
    try:
        import planegcs  # noqa: F401
    except ImportError:
        pytest.fail("COALESCE_REQUIRE_SKETCH=1 but planegcs is not installed", pytrace=False)
else:
    pytest.importorskip("planegcs")

from coalesce.sketch_solver import (
    OPERAND_SIGNATURES,
    Arc,
    BranchLandmark,
    Circle,
    Constraint,
    ConstraintKind,
    Dimension,
    DimensionKind,
    Line,
    Point,
    SolvedArc,
    SolvedCircle,
    SolvedPoint,
    SolveRequest,
    SolveResult,
    SolveStatus,
    solve_sketch,
)


def fixed(point_id: str, constraint_id: str) -> Constraint:
    return Constraint(constraint_id, ConstraintKind.FIX, (point_id,))


def rectangle_request(*, fixed_corner: bool) -> SolveRequest:
    raw = (
        ("p0", 1.0, 2.0),
        ("p1", 31.2, 2.1),
        ("p2", 31.0, 2.2),
        ("p3", 31.1, 22.0),
        ("p4", 30.9, 22.1),
        ("p5", 1.1, 21.9),
        ("p6", 0.9, 22.0),
        ("p7", 1.0, 2.1),
    )
    points = tuple(Point(point_id, x, y) for point_id, x, y in raw)
    lines = tuple(Line(f"line{i}", f"p{2 * i}", f"p{2 * i + 1}") for i in range(4))
    constraints = [
        Constraint(
            f"join{i}", ConstraintKind.COINCIDENT, (f"p{2 * i + 1}", f"p{(2 * (i + 1)) % 8}")
        )
        for i in range(4)
    ]
    constraints.extend(
        Constraint(f"axis{i}", kind, (f"line{i}",))
        for i, kind in enumerate(
            (
                ConstraintKind.HORIZONTAL,
                ConstraintKind.VERTICAL,
                ConstraintKind.HORIZONTAL,
                ConstraintKind.VERTICAL,
            )
        )
    )
    if fixed_corner:
        constraints.append(fixed("p0", "origin"))
    return SolveRequest(
        points=points,
        lines=lines,
        dimensions=(
            Dimension("width", DimensionKind.DISTANCE, ("p0", "p1"), 30.0),
            Dimension("height", DimensionKind.DISTANCE, ("p2", "p3"), 20.0),
        ),
        constraints=tuple(constraints),
    )


def test_missing_solver_extra_has_actionable_install_error(monkeypatch: pytest.MonkeyPatch):
    real_import = builtins.__import__

    def import_without_planegcs(name, *args, **kwargs):
        if name == "planegcs":
            raise ModuleNotFoundError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_planegcs)

    with pytest.raises(ImportError, match=r"pip install 'coalesce\[sketch\]'") as error:
        solve_sketch(SolveRequest())
    if sys.platform == "darwin":
        assert "brew install eigen boost" in str(error.value)


def test_rectangle_coordinates_dof_and_repeated_solves_are_deterministic():
    runs = [solve_sketch(rectangle_request(fixed_corner=False)) for _ in range(3)]

    assert [result.status for result in runs] == [SolveStatus.UNDER_CONSTRAINED] * 3
    assert [result.degrees_of_freedom for result in runs] == [2, 2, 2]
    coordinates = [
        tuple((entity_id, result.entities[entity_id]) for entity_id in sorted(result.entities))
        for result in runs
    ]
    assert coordinates[0] == coordinates[1] == coordinates[2]

    fixed_result = solve_sketch(rectangle_request(fixed_corner=True))
    assert fixed_result.status is SolveStatus.FULLY_CONSTRAINED
    assert fixed_result.degrees_of_freedom == 0
    assert fixed_result.entities["p0"] == SolvedPoint(1.0, 2.0)
    assert fixed_result.entities["p1"] == SolvedPoint(31.0, 2.0)


def test_under_full_redundant_and_conflicting_diagnostics_name_stable_ids():
    under = solve_sketch(SolveRequest(points=(Point("p", 0, 0),)))
    full = solve_sketch(
        SolveRequest(points=(Point("p", 0, 0),), constraints=(fixed("p", "fix-p"),))
    )
    base = {
        "points": (Point("p", 0, 0), Point("q", 5, 0)),
        "lines": (Line("edge", "p", "q"),),
        "constraints": (fixed("p", "fix-p"), fixed("q", "fix-q")),
    }
    redundant = solve_sketch(
        SolveRequest(
            **base,
            dimensions=(Dimension("distance-5", DimensionKind.DISTANCE, ("p", "q"), 5),),
        )
    )
    conflicting = solve_sketch(
        SolveRequest(
            **base,
            dimensions=(Dimension("distance-10", DimensionKind.DISTANCE, ("p", "q"), 10),),
        )
    )

    assert (under.status, under.degrees_of_freedom) == (SolveStatus.UNDER_CONSTRAINED, 2)
    assert (full.status, full.degrees_of_freedom) == (SolveStatus.FULLY_CONSTRAINED, 0)
    assert redundant.status is SolveStatus.REDUNDANT
    assert redundant.redundant_constraints
    assert set(redundant.redundant_constraints) <= {"fix-p", "fix-q", "distance-5"}
    assert conflicting.status is SolveStatus.CONFLICTING
    assert "distance-10" in conflicting.conflicting_constraints


def triangle_request(apex_y: float, expected_sign: int) -> SolveRequest:
    return SolveRequest(
        points=(Point("a", 0, 0), Point("b", 6, 0), Point("apex", 3, apex_y)),
        lines=(Line("base", "a", "b"), Line("left", "a", "apex"), Line("right", "b", "apex")),
        dimensions=(
            Dimension("left-length", DimensionKind.DISTANCE, ("a", "apex"), 6),
            Dimension("right-length", DimensionKind.DISTANCE, ("b", "apex"), 6),
        ),
        constraints=(fixed("a", "fix-a"), fixed("b", "fix-b")),
        landmarks=(
            BranchLandmark(
                "apex-side",
                "a",
                "b",
                "apex",
                expected_sign,
                ("left-length", "right-length"),
            ),
        ),
    )


def test_branch_flip_fails_closed_and_names_related_constraints():
    accepted = solve_sketch(triangle_request(4, 1))
    flipped = solve_sketch(triangle_request(-4, 1))

    assert accepted.status is SolveStatus.FULLY_CONSTRAINED
    assert accepted.landmarks["apex-side"] > 0
    assert flipped.status is SolveStatus.BRANCH_FLIP
    assert flipped.entities == {}
    assert flipped.errors[0].code == "BRANCH_FLIP"
    assert flipped.errors[0].constraint_ids == ("left-length", "right-length")


def test_arc_and_circle_radius_and_tangent_constraints_solve():
    arc_result = solve_sketch(
        SolveRequest(
            points=(
                Point("origin", 0, 0),
                Point("tangent", 0, 20),
                Point("end", -20, 0),
                Point("center", -20, 20),
            ),
            lines=(Line("side", "origin", "tangent"), Line("base", "origin", "end")),
            arcs=(Arc("corner", "center", "tangent", "end", 20, 0, 3 * math.pi / 2),),
            dimensions=(
                Dimension("side-length", DimensionKind.DISTANCE, ("origin", "tangent"), 20),
                Dimension("corner-radius", DimensionKind.RADIUS, ("corner",), 20),
            ),
            constraints=(
                fixed("origin", "fix-origin"),
                Constraint("base-horizontal", ConstraintKind.HORIZONTAL, ("base",)),
                Constraint("equal-sides", ConstraintKind.EQUAL, ("side", "base")),
                Constraint("side-tangent", ConstraintKind.TANGENT, ("side", "corner", "tangent")),
            ),
        )
    )
    circle_result = solve_sketch(
        SolveRequest(
            points=(Point("center", 3, 4),),
            circles=(Circle("hole", "center", 2.8),),
            dimensions=(Dimension("diameter", DimensionKind.DIAMETER, ("hole",), 6),),
            constraints=(fixed("center", "fix-center"),),
        )
    )

    assert arc_result.status is SolveStatus.FULLY_CONSTRAINED
    assert isinstance(arc_result.entities["corner"], SolvedArc)
    assert arc_result.entities["corner"].start == (0.0, 20.0)
    assert arc_result.entities["corner"].end == (-20.0, 0.0)
    assert math.isclose(arc_result.entities["corner"].radius, 20, abs_tol=1e-7)
    assert math.isclose(arc_result.entities["corner"].start_angle, 0, abs_tol=1e-7)
    assert math.isclose(arc_result.entities["corner"].end_angle, 3 * math.pi / 2, abs_tol=1e-7)
    assert circle_result.status is SolveStatus.FULLY_CONSTRAINED
    assert circle_result.entities["hole"] == SolvedCircle((3.0, 4.0), 3.0)


def test_40_entity_solve_is_fast_enough_for_interactive_use():
    points: list[Point] = []
    lines: list[Line] = []
    dimensions: list[Dimension] = []
    constraints: list[Constraint] = []
    for n in range(10):
        x, y = (n % 5) * 15.0, (n // 5) * 15.0
        point_ids = [f"r{n}p{i}" for i in range(4)]
        points.extend(
            (
                Point(point_ids[0], x, y),
                Point(point_ids[1], x + 10.1, y + 0.1),
                Point(point_ids[2], x + 10, y + 8.1),
                Point(point_ids[3], x, y + 8),
            )
        )
        for i in range(4):
            line_id = f"r{n}l{i}"
            lines.append(Line(line_id, point_ids[i], point_ids[(i + 1) % 4]))
            constraints.append(
                Constraint(
                    f"r{n}axis{i}",
                    ConstraintKind.HORIZONTAL if i % 2 == 0 else ConstraintKind.VERTICAL,
                    (line_id,),
                )
            )
        constraints.append(fixed(point_ids[0], f"r{n}fix"))
        dimensions.extend(
            (
                Dimension(f"r{n}width", DimensionKind.DISTANCE, (point_ids[0], point_ids[1]), 10),
                Dimension(f"r{n}height", DimensionKind.DISTANCE, (point_ids[1], point_ids[2]), 8),
            )
        )
    request = SolveRequest(
        points=tuple(points),
        lines=tuple(lines),
        dimensions=tuple(dimensions),
        constraints=tuple(constraints),
    )

    started = time.perf_counter()
    result = solve_sketch(request)
    elapsed = time.perf_counter() - started

    assert result.status is SolveStatus.FULLY_CONSTRAINED
    assert len(lines) == 40
    assert elapsed < 5


def test_solved_profile_extrudes_to_valid_build123d_solid():
    from build123d import Face, Solid, Vector, Wire
    from build123d import Plane as BuildPlane

    rectangle = solve_sketch(rectangle_request(fixed_corner=True))
    circle = solve_sketch(
        SolveRequest(
            points=(Point("center", 16, 12),),
            circles=(Circle("hole", "center", 2.8),),
            dimensions=(Dimension("radius", DimensionKind.RADIUS, ("hole",), 3),),
            constraints=(fixed("center", "fix-center"),),
        )
    )
    corners = [rectangle.entities[point_id] for point_id in ("p0", "p1", "p3", "p5")]
    hole = circle.entities["hole"]
    assert all(isinstance(point, SolvedPoint) for point in corners)
    assert isinstance(hole, SolvedCircle)
    outer = Wire.make_polygon([(point.x, point.y, 0) for point in corners + corners[:1]])
    inner = Wire.make_circle(hole.radius, BuildPlane((*hole.center, 0)))

    solid = Solid.extrude(Face(outer, [inner]), Vector(0, 0, 5))

    assert solid.is_valid
    assert math.isclose(solid.volume, (30 * 20 - math.pi * 3**2) * 5, rel_tol=1e-9)


@pytest.mark.parametrize(
    ("sketch_request", "responsible_id"),
    (
        (SolveRequest(points=(Point("p", 0, 0),), lines=(Line("same", "p", "p"),)), "same"),
        (
            SolveRequest(
                points=(Point("p", 0, 0), Point("q", 0, 0)),
                lines=(Line("coincident", "p", "q"),),
            ),
            "coincident",
        ),
        (SolveRequest(points=(Point("p", 0, 0),), initial={"p": (1,)}), "p"),
        (SolveRequest(points=(Point("p", 0, 0),), initial={"p": None}), "p"),
        (
            SolveRequest(constraints=(Constraint("bad-entities", ConstraintKind.FIX, None),)),
            "bad-entities",
        ),
        (SolveRequest(lines=(Line("bad-reference", [], "p"),)), "bad-reference"),
        (
            SolveRequest(
                points=(Point("p", 0, 0),),
                arcs=(Arc("bad-angle", "p", "p", "p", 1, "zero", 1),),
            ),
            "bad-angle",
        ),
        (
            SolveRequest(
                points=(Point("c", 0, 0), Point("s", 1, 0), Point("e", 0, 1)),
                arcs=(Arc("a", "c", "s", "e", 1, 0, math.pi / 2),),
                initial={"a": (1, 0)},
            ),
            "a",
        ),
        (SolveRequest(points=(Point("p", 0, 0),), initial={"missing": (1, 2)}), "missing"),
        (
            SolveRequest(
                points=(Point("p", 0, 0), Point("q", 1, 0)),
                lines=(Line("line", "p", "q"),),
                constraints=(Constraint("fix-line", ConstraintKind.FIX, ("line",)),),
            ),
            "fix-line",
        ),
        (SolveRequest(lines=(Line("unknown-point", "p", "q"),)), "unknown-point"),
        (SolveRequest(points=(Point("duplicate", 0, 0), Point("duplicate", 1, 0))), "duplicate"),
    ),
)
def test_invalid_geometry_is_structured_and_names_the_responsible_id(
    sketch_request, responsible_id
):
    result = solve_sketch(sketch_request)

    assert result.status is SolveStatus.INVALID_INPUT
    error = result.errors[0]
    assert responsible_id in (*error.entity_ids, *error.constraint_ids)


@pytest.mark.parametrize("kind", tuple(DimensionKind))
def test_every_dimension_family_rejects_a_bad_signature_with_its_id(kind):
    result = solve_sketch(
        SolveRequest(
            points=(Point("p", 0, 0),),
            dimensions=(Dimension("bad-dimension", kind, (), 1),),
        )
    )

    assert result.status is SolveStatus.INVALID_INPUT
    assert result.errors[0].constraint_ids == ("bad-dimension",)


@pytest.mark.parametrize("kind", tuple(ConstraintKind))
def test_every_constraint_family_rejects_a_bad_signature_with_its_id(kind):
    result = solve_sketch(
        SolveRequest(
            points=(Point("p", 0, 0),),
            constraints=(Constraint("bad-constraint", kind, ()),),
        )
    )

    assert result.status is SolveStatus.INVALID_INPUT
    assert result.errors[0].constraint_ids == ("bad-constraint",)


def test_angle_dimension_is_signed_counterclockwise_in_radians():
    result = solve_sketch(
        SolveRequest(
            points=(
                Point("a", 0, 0),
                Point("b", 1, 0),
                Point("c", 0, 0),
                Point("d", 0, -1),
            ),
            lines=(Line("first", "a", "b"), Line("second", "c", "d")),
            dimensions=(
                Dimension(
                    "clockwise-quarter-turn", DimensionKind.ANGLE, ("first", "second"), -math.pi / 2
                ),
            ),
            constraints=tuple(fixed(point_id, f"fix-{point_id}") for point_id in "abcd"),
        )
    )

    assert result.status is SolveStatus.REDUNDANT
    assert result.entities["second"].end == (0.0, -1.0)


@pytest.mark.parametrize(
    "constraint",
    (
        Constraint("horizontal-points", ConstraintKind.HORIZONTAL_POINTS, ("a", "b")),
        Constraint("vertical-points", ConstraintKind.VERTICAL_POINTS, ("a", "b")),
        Constraint("parallel", ConstraintKind.PARALLEL, ("first", "second")),
        Constraint("perpendicular", ConstraintKind.PERPENDICULAR, ("first", "second")),
        Constraint("symmetric-point", ConstraintKind.SYMMETRIC, ("a", "b", "c")),
        Constraint("symmetric-line", ConstraintKind.SYMMETRIC, ("a", "b", "first")),
        Constraint("point-on-line", ConstraintKind.POINT_ON, ("c", "first")),
    ),
)
def test_remaining_constraint_families_dispatch_valid_signatures(constraint):
    result = solve_sketch(
        SolveRequest(
            points=(Point("a", 0, 0), Point("b", 2, 1), Point("c", 1, 0)),
            lines=(Line("first", "a", "b"), Line("second", "a", "c")),
            constraints=(constraint,),
        )
    )

    assert result.status is not SolveStatus.INVALID_INPUT


def test_tangent_endpoint_must_be_shared_by_the_line_and_arc():
    result = solve_sketch(
        SolveRequest(
            points=(
                Point("center", 0, 0),
                Point("arc-start", 1, 0),
                Point("shared", 0, 1),
                Point("line-start", -1, 1),
                Point("other", 2, 2),
            ),
            lines=(Line("line", "line-start", "shared"),),
            arcs=(Arc("arc", "center", "arc-start", "shared", 1, 0, math.pi / 2),),
            constraints=(Constraint("tangent", ConstraintKind.TANGENT, ("line", "arc", "other")),),
        )
    )

    assert result.status is SolveStatus.INVALID_INPUT
    assert result.errors[0].constraint_ids == ("tangent",)


def test_invalid_input_fuzz_never_raises_raw_exceptions():
    rng = random.Random(0xC0A1E5CE)
    bad_ids = (None, True, 1, float("nan"), [], {})
    bad_numbers = (None, True, "1", float("nan"), float("inf"), [], {})
    bad_sequences = (None, True, 1, {})
    requests = []
    for _ in range(300):
        case = rng.randrange(12)
        if case == 0:
            request = SolveRequest(points=None)
        elif case == 1:
            request = SolveRequest(points=(Point(rng.choice(bad_ids), 0, 0),))
        elif case == 2:
            request = SolveRequest(points=(Point("p", rng.choice(bad_numbers), 0),))
        elif case == 3:
            request = SolveRequest(lines=(Line("line", rng.choice(bad_ids), "p"),))
        elif case == 4:
            request = SolveRequest(
                points=(Point("p", 0, 0),),
                arcs=(Arc("arc", "p", "p", "p", 1, rng.choice(bad_numbers), 1),),
            )
        elif case == 5:
            request = SolveRequest(circles=(Circle("circle", rng.choice(bad_ids), 1),))
        elif case == 6:
            request = SolveRequest(
                points=(Point("p", 0, 0),),
                dimensions=(
                    Dimension("dimension", DimensionKind.DISTANCE, rng.choice(bad_sequences), 1),
                ),
            )
        elif case == 7:
            request = SolveRequest(
                constraints=(
                    Constraint("constraint", ConstraintKind.FIX, rng.choice(bad_sequences)),
                )
            )
        elif case == 8:
            request = SolveRequest(
                points=(Point("p", 0, 0),),
                initial={"p": rng.choice(bad_sequences)},
            )
        elif case == 9:
            request = SolveRequest(initial=rng.choice((None, True, 1, [])))
        elif case == 10:
            request = SolveRequest(
                landmarks=(BranchLandmark("landmark", rng.choice(bad_ids), "b", "p", 1),)
            )
        else:
            request = SolveRequest(
                points=(Point("p", 0, 0),),
                constraints=(Constraint("constraint", rng.choice(bad_sequences), ("p",)),),
            )
        requests.append(request)

    results = [solve_sketch(request) for request in requests]

    assert all(isinstance(result, SolveResult) for result in results)
    assert all(result.status is SolveStatus.INVALID_INPUT for result in results)
    assert all(result.errors and result.errors[0].code for result in results)


def test_documented_operand_table_matches_machine_readable_signatures():
    docs = (Path(__file__).parents[1] / "docs" / "sketch.md").read_text()
    documented: defaultdict[str, set[tuple[str, ...]]] = defaultdict(set)
    for line in docs.splitlines():
        if not line.startswith("| `") or "ordered operands" in line:
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        kind_match = re.fullmatch(r"`([^`]+)`(?: with an endpoint)?", cells[0])
        if kind_match is None:
            continue
        for operands in re.findall(r"\(([^()]*)\)", cells[1]):
            parsed = tuple(operand.strip() for operand in operands.split(","))
            if set(parsed) <= {"point", "line", "arc", "circle"}:
                documented[kind_match.group(1)].add(parsed)

    implemented = {
        kind.value: {signature.operands for signature in signatures}
        for kind, signatures in OPERAND_SIGNATURES.items()
    }
    assert dict(documented) == implemented


def test_only_solver_seam_may_import_planegcs():
    source_root = Path(__file__).parents[1] / "src" / "coalesce"
    offenders = []
    for path in source_root.rglob("*.py"):
        if path.name == "sketch_solver.py":
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import) and any(
                alias.name == "planegcs" for alias in node.names
            ):
                offenders.append(path)
            if isinstance(node, ast.ImportFrom) and node.module == "planegcs":
                offenders.append(path)
    assert offenders == []


@pytest.mark.parametrize(
    "sketch_request",
    (
        SolveRequest(points=(Point("huge", 10**10000, 0),)),
        SolveRequest(points=(Point("p", 0, 0),), initial={"p": (10**400, 0)}),
        SolveRequest(
            points=(Point("p", 0, 0), Point("q", 1, 0)),
            dimensions=(Dimension("d", DimensionKind.DISTANCE, ("p", "q"), 10**400),),
        ),
    ),
)
def test_huge_integers_are_invalid_input_not_overflow(sketch_request):
    assert solve_sketch(sketch_request).status is SolveStatus.INVALID_INPUT


@pytest.mark.parametrize("error_type", (TypeError, ValueError, RuntimeError))
def test_solver_failures_are_not_reported_as_caller_input_errors(monkeypatch, error_type):
    import planegcs

    def broken(*_args, **_kwargs):
        raise error_type("solver defect")

    monkeypatch.setattr(planegcs.Sketch, "solve", broken)
    with pytest.raises(error_type, match="solver defect"):
        solve_sketch(rectangle_request(fixed_corner=True))
