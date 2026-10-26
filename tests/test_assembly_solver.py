import json

import pytest
from build123d import GeomType
from test_mcp_assembly import occurrence_create, transform
from test_mcp_vertical import FEATURE, SKETCH

from coalesce.assembly import locate_shape
from coalesce.service import DocumentService, _scene_parts


def _setup_boxes(path):
    service = DocumentService(path, warmup=False)
    assert service.sketch(SKETCH)["ok"]
    assert service.feature(FEATURE)["ok"]
    assert service.feature(occurrence_create(2, "place-fixed", "fixed"))["ok"]
    moved = occurrence_create(3, "place-moved", "moved", 40)
    moved["parameters"]["transform"] = transform(40, (1, 1, 0), 67)
    assert service.feature(moved)["ok"]
    return service


def _face_reference(service, role):
    result = service.query({"select": {"kind": "face_roles", "id": "plate_extrude"}})
    return next(item["exampleReference"] for item in result["roles"] if item["role"] == role)


def _concentric_setup(path):
    service = DocumentService(path, warmup=False)
    sketch = {
        **SKETCH,
        "entities": [
            {
                "id": "outline",
                "kind": "circle",
                "center": [0, 0],
                "radius": {"value": 8, "unit": "mm"},
                "rotation": {"value": 0, "unit": "deg"},
            }
        ],
    }
    assert service.sketch(sketch)["ok"]
    assert service.feature(FEATURE)["ok"]
    fixed = occurrence_create(2, "place-fixed", "fixed")
    fixed["parameters"]["transform"] = transform(13, (1, 2, 3), 41)
    moved = occurrence_create(3, "place-moved", "moved", 40)
    moved["parameters"]["transform"] = transform(40, (2, -1, 3), 123)
    assert service.feature(fixed)["ok"]
    assert service.feature(moved)["ok"]
    return service


def _concentric_create(revision, first, second, *, orientation="aligned", mate_id="coaxial"):
    return {
        "action": "create",
        "kind": "assembly_mate",
        "expectedRevision": revision,
        "requestId": f"create-{mate_id}",
        "mateId": mate_id,
        "parameters": {
            "kind": "concentric",
            "first": {"occurrenceId": "fixed", "reference": first},
            "second": {"occurrenceId": "moved", "reference": second},
            "orientation": orientation,
        },
    }


def _axis(face):
    axis = face.axis_of_rotation
    return tuple(axis.position), tuple(axis.direction)


def _cross_for_test(axis, point, origin):
    delta = tuple(b - a for a, b in zip(origin, point, strict=True))
    return tuple(
        axis[(index + 1) % 3] * delta[(index + 2) % 3]
        - axis[(index + 2) % 3] * delta[(index + 1) % 3]
        for index in range(3)
    )


def _planar_create(revision, first, second, *, offset=0, orientation="opposed"):
    return {
        "action": "create",
        "kind": "assembly_mate",
        "expectedRevision": revision,
        "requestId": "seat-boxes",
        "mateId": "box-seat",
        "parameters": {
            "kind": "planar",
            "first": {"occurrenceId": "fixed", "reference": first},
            "second": {"occurrenceId": "moved", "reference": second},
            "orientation": orientation,
            "offset": {"value": offset, "unit": "mm"},
        },
    }


def _plane(face):
    center = face.center()
    normal = face.normal_at()
    return (center.X, center.Y, center.Z), (normal.X, normal.Y, normal.Z)


def test_face_roles_discover_exact_typed_planar_references(tmp_path):
    service = _setup_boxes(tmp_path / "face-roles.coalesce.json")

    result = service.query({"select": {"kind": "face_roles", "id": "plate_extrude"}})

    cap = next(item for item in result["roles"] if item["role"] == "cap_end")
    assert cap["faceCount"] == 1
    assert cap["surfaceType"] == "PLANE"
    assert cap["area"]["value"] == pytest.approx(2400)
    assert cap["frame"]["normal"] == pytest.approx([0, 0, 1])
    assert cap["exampleReference"]["cardinality"] == "one"
    assert (
        cap["exampleReference"]["source"]["kind"] == "sketch_segment"
        or cap["exampleReference"]["source"]["kind"] == "tool_cap"
    )


@pytest.mark.parametrize(
    ("orientation", "offset", "normal_sign"),
    [("opposed", 7, -1), ("aligned", -25, 1)],
)
def test_planar_mate_places_boxes_exactly_with_signed_offset_and_reports_three_dof(
    tmp_path, orientation, offset, normal_sign
):
    service = _setup_boxes(tmp_path / "planar.coalesce.json")
    first = _face_reference(service, "cap_end")
    second = _face_reference(service, "cap_start")

    result = service.feature(
        _planar_create(4, first, second, offset=offset, orientation=orientation)
    )

    assert result["ok"]
    query = service.query({"select": {"kind": "assembly"}})
    moved = next(item for item in query["occurrences"] if item["occurrenceId"] == "moved")
    assert moved["remainingDof"] == 3
    by_id = {item.occurrence.id: item for item in service._resolved_assembly()}
    fixed_face = next(
        face
        for face in locate_shape(
            by_id["fixed"].part.shape, by_id["fixed"].occurrence.transform
        ).faces()
        if face.geom_type is GeomType.PLANE and face.normal_at().Z > 0.9
    )
    moved_face = next(
        face
        for face in locate_shape(
            by_id["moved"].part.shape, by_id["moved"].occurrence.transform
        ).faces()
        if face.geom_type is GeomType.PLANE and face.normal_at().Z * normal_sign > 0.9
    )
    first_center, first_normal = _plane(fixed_face)
    second_center, second_normal = _plane(moved_face)
    assert sum(a * b for a, b in zip(first_normal, second_normal, strict=True)) == pytest.approx(
        normal_sign
    )
    assert sum(
        (b - a) * n for a, b, n in zip(first_center, second_center, first_normal, strict=True)
    ) == pytest.approx(offset)


def test_rotated_planar_transform_survives_json_restart_exactly(tmp_path):
    from test_mcp_assembly import ground_create

    path = tmp_path / "rotated-restart.coalesce.json"
    service = DocumentService(path, warmup=False)
    assert service.sketch(SKETCH)["ok"]
    assert service.feature(FEATURE)["ok"]
    fixed = occurrence_create(2, "place-fixed", "fixed")
    fixed["parameters"]["transform"] = transform(0, (1, 2, 3), 41)
    moved = occurrence_create(3, "place-moved", "moved")
    moved["parameters"]["transform"] = transform(0, (2, -1, 3), 123)
    assert service.feature(fixed)["ok"]
    assert service.feature(moved)["ok"]
    assert service.feature(ground_create(4, "ground-fixed", "ground-fixed", "fixed"))["ok"]
    first = _face_reference(service, "cap_end")
    second = _face_reference(service, "cap_start")
    assert service.feature(_planar_create(5, first, second, offset=-13.25, orientation="aligned"))[
        "ok"
    ]
    before = service.query({"select": {"kind": "assembly"}})

    restarted = DocumentService(path, warmup=False)
    after = restarted.query({"select": {"kind": "assembly"}})

    assert after["ok"]
    assert after == before


def test_planar_transform_is_byte_stable_across_restart_and_used_by_export_and_interference(
    tmp_path,
):
    path = tmp_path / "restart.coalesce.json"
    service = _setup_boxes(path)
    first = _face_reference(service, "cap_end")
    second = _face_reference(service, "cap_start")
    assert service.feature(_planar_create(4, first, second, offset=5))["ok"]
    before = path.read_bytes()
    transform_before = next(
        item for item in json.loads(before)["assembly"]["occurrences"] if item["id"] == "moved"
    )["transform"]

    restarted = DocumentService(path, warmup=False)
    query = restarted.query({"select": {"kind": "assembly"}})
    transform_after = next(
        item for item in query["occurrences"] if item["occurrenceId"] == "moved"
    )["transform"]
    assert path.read_bytes() == before
    assert transform_after["translation"]["value"] == transform_before["translation"]
    inspection = restarted.check(
        {
            "action": "evaluate",
            "requirements": [
                {
                    "id": "gap",
                    "kind": "assembly_interference",
                    "occurrenceIds": ["fixed", "moved"],
                    "minimumClearance": {"value": 5, "unit": "mm"},
                }
            ],
        }
    )
    assert inspection["results"][0]["status"] == "PASS"
    preflight = restarted.export({"action": "preflight", "mode": "assembly", "expectedRevision": 5})
    assert preflight["ok"]
    assert preflight["occurrenceIds"] == ["fixed", "moved"]
    destination = tmp_path / "solved.step"
    published = restarted.export(
        {
            "action": "publish",
            "mode": "assembly",
            "expectedRevision": 5,
            "path": str(destination),
        }
    )
    assert published["ok"]
    assert destination.exists()
    exported_moved = next(
        item
        for item in published["validation"]["xcaf"]["occurrences"]
        if item["occurrenceId"] == "moved"
    )
    assert exported_moved["transform"]["translation"] == transform_before["translation"]


def test_stale_face_reference_after_feature_edit_is_atomic_and_names_mate(tmp_path):
    path = tmp_path / "stale.coalesce.json"
    service = _setup_boxes(path)
    first = _face_reference(service, "side(right)")
    second = _face_reference(service, "side(left)")
    assert service.feature(_planar_create(4, first, second))["ok"]
    before = path.read_bytes()
    update = {
        "action": "update",
        "expectedRevision": 5,
        "requestId": "replace-referenced-segments",
        "sketchId": "plate_profile",
        "set": {
            "entities": [
                {
                    "id": "outline",
                    "kind": "circle",
                    "center": [0, 0],
                    "radius": {"value": 20, "unit": "mm"},
                    "rotation": {"value": 0, "unit": "deg"},
                }
            ]
        },
    }

    result = service.sketch(update)

    assert result["error"]["code"] == "MATE_REFERENCE_STALE"
    assert result["error"]["mateId"] == "box-seat"
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    ("first_id", "first_offset", "second_id", "second_offset"),
    [("m1", 5, "m2", 7), ("m2", 7, "m1", 5)],
)
def test_conflicting_planar_cycle_reports_every_mate_independent_of_id_order(
    tmp_path, first_id, first_offset, second_id, second_offset
):
    service = _setup_boxes(tmp_path / f"conflict-{first_id}.coalesce.json")
    first = _face_reference(service, "cap_end")
    second = _face_reference(service, "cap_start")
    initial = _planar_create(4, first, second, offset=first_offset, orientation="aligned")
    initial["mateId"] = first_id
    initial["requestId"] = f"create-{first_id}"
    assert service.feature(initial)["ok"]
    conflicting = _planar_create(5, first, second, offset=second_offset, orientation="aligned")
    conflicting["mateId"] = second_id
    conflicting["requestId"] = f"create-{second_id}"

    result = service.feature(conflicting)

    assert result["error"]["code"] == "MATE_CONFLICT"
    assert result["error"]["mateIds"] == ["m1", "m2"]
    assert result["error"]["linearResidual"]["unit"] == "mm"
    assert result["error"]["linearResidual"]["value"] == pytest.approx(2)
    assert result["error"]["angularResidual"]["unit"] == "deg"


def test_explicit_transform_update_warns_when_planar_constraints_override_it(tmp_path):
    service = _setup_boxes(tmp_path / "constrained-update.coalesce.json")
    first = _face_reference(service, "cap_end")
    second = _face_reference(service, "cap_start")
    assert service.feature(_planar_create(4, first, second, orientation="aligned"))["ok"]
    update = {
        "action": "update",
        "kind": "assembly_occurrence",
        "expectedRevision": 5,
        "requestId": "move-constrained",
        "occurrenceId": "moved",
        "set": {"transform": transform(80, (2, -1, 3), 123)},
    }

    result = service.feature(update)

    assert result["ok"]
    assert result["warnings"] == [
        {
            "code": "MATED_TRANSFORM_OVERRIDDEN",
            "occurrenceId": "moved",
            "constrainedCoordinates": ["normalAlignment", "normalOffset"],
            "freeMotions": ["inPlaneX", "inPlaneY", "normalSpin"],
        }
    ]
    assert result["transform"] != update["set"]["transform"]


def test_duplicate_planar_restriction_reports_redundancy(tmp_path):
    service = _setup_boxes(tmp_path / "redundant.coalesce.json")
    first = _face_reference(service, "cap_end")
    second = _face_reference(service, "cap_start")
    assert service.feature(_planar_create(4, first, second))["ok"]
    duplicate = _planar_create(5, first, second)
    duplicate["requestId"] = "second-seat"
    duplicate["mateId"] = "box-seat-2"

    assert service.feature(duplicate)["ok"]
    assert service.query({"select": {"kind": "assembly"}})["redundantMateIds"] == ["box-seat-2"]


def test_shared_mate_graph_rejects_two_grounds_when_planar_connects_components(tmp_path):
    from test_mcp_assembly import ground_create

    path = tmp_path / "ground-graph.coalesce.json"
    service = _setup_boxes(path)
    assert service.feature(ground_create(4, "ground-fixed", "ground-fixed", "fixed"))["ok"]
    assert service.feature(ground_create(5, "ground-moved", "ground-moved", "moved"))["ok"]
    before = path.read_bytes()
    planar = _planar_create(
        6,
        _face_reference(service, "cap_end"),
        _face_reference(service, "cap_start"),
    )

    result = service.feature(planar)

    assert result["error"]["code"] == "MATE_CONFLICT"
    assert result["error"]["mateIds"] == ["ground-fixed", "ground-moved"]
    assert path.read_bytes() == before


def test_planar_mate_rejects_non_planar_face_with_structured_details(tmp_path):
    sketch = {
        **SKETCH,
        "entities": [
            {
                "id": "outline",
                "kind": "circle",
                "center": [0, 0],
                "radius": {"value": 20, "unit": "mm"},
                "rotation": {"value": 0, "unit": "deg"},
            }
        ],
    }
    service = DocumentService(tmp_path / "cylinders.coalesce.json", warmup=False)
    assert service.sketch(sketch)["ok"]
    assert service.feature(FEATURE)["ok"]
    assert service.feature(occurrence_create(2, "place-fixed", "fixed"))["ok"]
    assert service.feature(occurrence_create(3, "place-moved", "moved", 50))["ok"]
    roles = service.query({"select": {"kind": "face_roles", "id": "plate_extrude"}})["roles"]
    cylinder = next(role["exampleReference"] for role in roles if role["surfaceType"] == "CYLINDER")
    planar = _planar_create(4, cylinder, cylinder)

    result = service.feature(planar)

    assert result["error"]["code"] == "MATE_GEOMETRY_UNSUPPORTED"
    assert result["error"]["mateId"] == "box-seat"
    assert result["error"]["surfaceType"] == "CYLINDER"


def test_planar_mate_rejects_zero_area_face_with_structured_details():
    from build123d import Box, Face

    from coalesce.assembly import (
        Assembly,
        AssemblyError,
        Mate,
        MateSide,
        Occurrence,
        ResolvedOccurrence,
        RigidTransform,
    )
    from coalesce.assembly_solver import solve_assembly
    from coalesce.parts import TerminalPart
    from coalesce.provenance import candidate
    from coalesce.references import BodySource, Reference

    tiny = Face.make_rect(1e-5, 1e-5)
    box = Box(1, 1, 1)
    box_face = max(box.faces(), key=lambda face: face.center().Z)
    first_body = BodySource("tiny", "face")
    second_body = BodySource("box", "solid")
    first_part = TerminalPart("tiny/face", "tiny", "manufacturing", first_body, "tiny", tiny)
    second_part = TerminalPart("box/solid", "box", "manufacturing", second_body, "box", box)
    identity = RigidTransform((0, 0, 0), (0, 0, 1), 0)
    first_occurrence = Occurrence("tiny", first_part.part_id, identity)
    second_occurrence = Occurrence("box", second_part.part_id, identity)
    first_reference = Reference("tiny", "plane", source="face", body=first_body)
    second_reference = Reference("box", "plane", source="face", body=second_body)
    mate = Mate(
        "seat",
        "planar",
        first=MateSide("tiny", first_reference),
        second=MateSide("box", second_reference),
        orientation="aligned",
    )
    assembly = Assembly(
        {"tiny": first_occurrence, "box": second_occurrence},
        {"seat": mate},
    )
    resolved = [
        ResolvedOccurrence(first_occurrence, first_part, tiny),
        ResolvedOccurrence(second_occurrence, second_part, box),
    ]
    provenance = {
        "tiny": [candidate("tiny", "plane", "face", tiny, body=first_body)],
        "box": [candidate("box", "plane", "face", box_face, body=second_body)],
    }

    with pytest.raises(AssemblyError) as error:
        solve_assembly(assembly, resolved, provenance)

    assert error.value.code == "MATE_GEOMETRY_UNSUPPORTED"
    assert error.value.details["mateId"] == "seat"
    assert error.value.details["area"]["value"] == pytest.approx(1e-10)


@pytest.mark.parametrize("status", ["EMPTY", "SPLIT", "MERGED", "AMBIGUOUS"])
def test_all_stale_reference_statuses_name_the_mate(monkeypatch, status):
    from build123d import Box

    from coalesce import assembly_solver
    from coalesce.assembly import Mate, MateSide, Occurrence, ResolvedOccurrence, RigidTransform
    from coalesce.parts import TerminalPart
    from coalesce.references import BodySource, Reference, Resolution, Status

    shape = Box(1, 1, 1)
    body = BodySource("box", "solid")
    part = TerminalPart("box/solid", "box", "manufacturing", body, "box", shape)
    occurrence = Occurrence("box", part.part_id, RigidTransform((0, 0, 0), (0, 0, 1), 0))
    resolved = ResolvedOccurrence(occurrence, part, shape)
    reference = Reference("box", "plane", body=body)
    mate = Mate(
        "stale-seat",
        "planar",
        first=MateSide("box", reference),
        second=MateSide("other", reference),
        orientation="aligned",
    )
    monkeypatch.setattr(
        assembly_solver,
        "resolve",
        lambda *_: Resolution(Status(status), error={"status": status}),
    )
    monkeypatch.setattr(
        assembly_solver,
        "face_role_catalog",
        lambda *_: [{"role": "plane", "faceCount": 1}],
    )

    with pytest.raises(assembly_solver.AssemblyError) as error:
        assembly_solver._resolved_face(mate, "first", resolved, {})

    assert error.value.code == "MATE_REFERENCE_STALE"
    assert error.value.details["mateId"] == "stale-seat"
    assert error.value.details["side"] == "first"
    assert error.value.details["occurrenceId"] == "box"
    assert error.value.details["resolution"]["status"] == status
    assert error.value.details["resolution"]["availableRoles"] == [
        {"role": "plane", "faceCount": 1}
    ]


@pytest.mark.parametrize(("orientation", "sign"), [("aligned", 1), ("opposed", -1)])
def test_concentric_mate_collinear_from_odd_rotations_and_restart_is_exact(
    tmp_path, orientation, sign
):
    path = tmp_path / f"concentric-{orientation}.coalesce.json"
    service = _concentric_setup(path)
    cylinder = _face_reference(service, "side(circle)")

    assert service.feature(_concentric_create(4, cylinder, cylinder, orientation=orientation))["ok"]
    before = path.read_bytes()
    query = service.query({"select": {"kind": "assembly"}})
    moved_status = next(row for row in query["occurrences"] if row["occurrenceId"] == "moved")
    assert moved_status["remainingDof"] == 2
    assert moved_status["freeMotions"] == ["axisTranslation", "axisSpin"]
    by_id = {item.occurrence.id: item for item in service._resolved_assembly()}
    axes = []
    for occurrence_id in ("fixed", "moved"):
        item = by_id[occurrence_id]
        shape = locate_shape(item.part.shape, item.occurrence.transform)
        axes.append(
            _axis(next(face for face in shape.faces() if face.geom_type is GeomType.CYLINDER))
        )
    delta = tuple(b - a for a, b in zip(axes[0][0], axes[1][0], strict=True))
    cross = tuple(
        axes[0][1][(index + 1) % 3] * delta[(index + 2) % 3]
        - axes[0][1][(index + 2) % 3] * delta[(index + 1) % 3]
        for index in range(3)
    )
    assert sum(value * value for value in cross) == pytest.approx(0, abs=1e-12)
    assert sum(a * b for a, b in zip(axes[0][1], axes[1][1], strict=True)) == pytest.approx(sign)

    restarted = DocumentService(path, warmup=False)
    assert restarted.query({"select": {"kind": "assembly"}}) == query
    assert path.read_bytes() == before


def test_concentric_plus_planar_seat_has_one_spin_dof_and_shared_outputs(tmp_path):
    path = tmp_path / "concentric-seat.coalesce.json"
    service = _concentric_setup(path)
    cylinder = _face_reference(service, "side(circle)")
    cap_end = _face_reference(service, "cap_end")
    cap_start = _face_reference(service, "cap_start")
    assert service.feature(_concentric_create(4, cylinder, cylinder))["ok"]
    seat = _planar_create(5, cap_end, cap_start, orientation="opposed")
    seat["mateId"] = "seat"
    seat["requestId"] = "create-seat"
    assert service.feature(seat)["ok"]

    query = service.query({"select": {"kind": "assembly"}})
    moved = next(row for row in query["occurrences"] if row["occurrenceId"] == "moved")
    assert moved["remainingDof"] == 1
    assert moved["freeMotions"] == ["axisSpin"]
    scene_occurrence = next(
        row
        for row in _scene_parts(service._regen, service._document.assembly)
        if row.occurrence_id == "moved"
    )
    assert scene_occurrence.transform == service._document.assembly.occurrences["moved"].transform
    inspection = service.check(
        {
            "action": "evaluate",
            "requirements": [
                {"id": "fit", "kind": "assembly_interference", "occurrenceIds": ["fixed", "moved"]}
            ],
        }
    )
    assert inspection["ok"]
    destination = tmp_path / "concentric-seat.step"
    exported = service.export(
        {"action": "publish", "mode": "assembly", "expectedRevision": 6, "path": str(destination)}
    )
    exported_transform = next(
        row
        for row in exported["validation"]["xcaf"]["occurrences"]
        if row["occurrenceId"] == "moved"
    )["transform"]
    assert exported_transform["translation"] == moved["transform"]["translation"]["value"]


def test_concentric_rejects_planar_face_with_role_catalog(tmp_path):
    service = _setup_boxes(tmp_path / "wrong-surface.coalesce.json")
    cap = _face_reference(service, "cap_end")
    result = service.feature(_concentric_create(4, cap, cap))
    assert result["error"]["code"] == "MATE_GEOMETRY_UNSUPPORTED"
    assert result["error"]["surfaceType"] == "PLANE"
    assert "availableRoles" in result["error"]


def test_concentric_transform_update_warns_about_axis_coordinates(tmp_path):
    service = _concentric_setup(tmp_path / "concentric-warning.coalesce.json")
    cylinder = _face_reference(service, "side(circle)")
    assert service.feature(_concentric_create(4, cylinder, cylinder))["ok"]
    update = {
        "action": "update",
        "kind": "assembly_occurrence",
        "expectedRevision": 5,
        "requestId": "move-concentric",
        "occurrenceId": "moved",
        "set": {"transform": transform(80, (2, -1, 3), 123)},
    }

    result = service.feature(update)

    assert result["ok"]
    assert result["warnings"][0]["constrainedCoordinates"] == [
        "axisAlignment",
        "radialOffset",
    ]
    assert result["warnings"][0]["freeMotions"] == ["axisTranslation", "axisSpin"]


def test_grounded_concentric_chain_is_independent_of_mate_input_order(tmp_path):
    from test_mcp_assembly import ground_create

    solved_transforms = []
    for reverse in (False, True):
        service = _concentric_setup(tmp_path / f"chain-{reverse}.coalesce.json")
        middle = occurrence_create(4, "place-middle", "middle", -25)
        middle["parameters"]["transform"] = transform(-25, (1, -3, 2), 73)
        assert service.feature(middle)["ok"]
        assert service.feature(ground_create(5, "ground-fixed", "ground-fixed", "fixed"))["ok"]
        cylinder = _face_reference(service, "side(circle)")
        relations = [("a", "fixed", "middle"), ("b", "middle", "moved")]
        if reverse:
            relations.reverse()
        for revision, (mate_id, first_id, second_id) in enumerate(relations, start=6):
            request = _concentric_create(revision, cylinder, cylinder, mate_id=mate_id)
            request["parameters"]["first"]["occurrenceId"] = first_id
            request["parameters"]["second"]["occurrenceId"] = second_id
            assert service.feature(request)["ok"]
        solved = service.query({"select": {"kind": "assembly"}})
        axes = []
        for item in service._resolved_assembly():
            shape = locate_shape(item.part.shape, item.occurrence.transform)
            cylinder_face = next(
                face for face in shape.faces() if face.geom_type is GeomType.CYLINDER
            )
            axes.append(_axis(cylinder_face))
        anchor_point, anchor_axis = axes[0]
        solved_transforms.append(
            [
                sum(value * value for value in _cross_for_test(anchor_axis, point, anchor_point))
                for point, _ in axes[1:]
            ]
        )
        assert solved["components"][0]["mateIds"] == ["a", "b", "ground-fixed"]

    assert solved_transforms == [[pytest.approx(0, abs=1e-12)] * 2] * 2


def test_coupled_mixed_neighbor_graph_fails_without_sequential_drift(tmp_path):
    service = _concentric_setup(tmp_path / "unsupported-coupling.coalesce.json")
    middle = occurrence_create(4, "place-middle", "middle", -25)
    assert service.feature(middle)["ok"]
    cylinder = _face_reference(service, "side(circle)")
    coaxial = _concentric_create(5, cylinder, cylinder)
    coaxial["parameters"]["second"]["occurrenceId"] = "middle"
    assert service.feature(coaxial)["ok"]
    before = (tmp_path / "unsupported-coupling.coalesce.json").read_bytes()
    seat = _planar_create(
        6,
        _face_reference(service, "cap_end"),
        _face_reference(service, "cap_start"),
    )
    seat["parameters"]["first"]["occurrenceId"] = "middle"
    result = service.feature(seat)

    assert result["error"]["code"] == "MATE_SOLVE_UNSUPPORTED"
    assert result["error"]["mateIds"] == ["box-seat", "coaxial"]
    assert (tmp_path / "unsupported-coupling.coalesce.json").read_bytes() == before


def test_named_hole_wall_concentric_reference_survives_diameter_edit(tmp_path):
    from test_mcp_cut_hole_circle import extrude, hole, rectangle_sketch

    service = DocumentService(tmp_path / "hole-wall.coalesce.json", warmup=False)
    assert service.sketch(
        rectangle_sketch(
            request_id="sketch",
            revision=0,
            sketch_id="plate_profile",
            entity_id="outline",
            width=40,
            height=40,
        )
    )["ok"]
    assert service.feature(
        extrude(
            request_id="plate",
            revision=1,
            feature_id="plate",
            sketch_id="plate_profile",
            profile_ids=["outline"],
            distance=8,
        )
    )["ok"]
    assert service.feature(
        hole(
            request_id="bore",
            revision=2,
            feature_id="bore",
            target="plate",
            position=(0, 0),
            diameter=8,
        )
    )["ok"]
    fixed = occurrence_create(3, "fixed", "fixed")
    fixed["parameters"]["partId"] = "plate/outline"
    moved = occurrence_create(4, "moved", "moved", 30)
    moved["parameters"]["partId"] = "plate/outline"
    assert service.feature(fixed)["ok"]
    assert service.feature(moved)["ok"]
    roles = service.query({"select": {"kind": "face_roles", "id": "bore"}})["roles"]
    wall = next(row["exampleReference"] for row in roles if row["surfaceType"] == "CYLINDER")
    assert wall["source"]["kind"] == "hole_surface"
    assert service.feature(_concentric_create(5, wall, wall))["ok"]
    edit = {
        "action": "update",
        "kind": "hole",
        "expectedRevision": 6,
        "requestId": "widen-bore",
        "featureId": "bore",
        "set": {"diameter": {"value": 10, "unit": "mm"}},
    }
    result = service.feature(edit)
    assert result["ok"]
    assert service.query({"select": {"kind": "assembly"}})["ok"]
