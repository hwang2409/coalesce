import json

import pytest
from build123d import GeomType
from test_mcp_assembly import occurrence_create, transform
from test_mcp_vertical import FEATURE, SKETCH

from coalesce.assembly import locate_shape
from coalesce.service import DocumentService


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
    assert cap["exampleReference"]["source"]["kind"] == "sketch_segment" or cap[
        "exampleReference"
    ]["source"]["kind"] == "tool_cap"


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
    fixed_face = next(face for face in locate_shape(by_id["fixed"].part.shape, by_id["fixed"].occurrence.transform).faces() if face.geom_type is GeomType.PLANE and face.normal_at().Z > 0.9)
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
        (b - a) * n
        for a, b, n in zip(first_center, second_center, first_normal, strict=True)
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
    assert service.feature(
        _planar_create(5, first, second, offset=-13.25, orientation="aligned")
    )["ok"]
    before = service.query({"select": {"kind": "assembly"}})

    restarted = DocumentService(path, warmup=False)
    after = restarted.query({"select": {"kind": "assembly"}})

    assert after["ok"]
    assert after == before


def test_planar_transform_is_byte_stable_across_restart_and_used_by_export_and_interference(tmp_path):
    path = tmp_path / "restart.coalesce.json"
    service = _setup_boxes(path)
    first = _face_reference(service, "cap_end")
    second = _face_reference(service, "cap_start")
    assert service.feature(_planar_create(4, first, second, offset=5))["ok"]
    before = path.read_bytes()
    transform_before = next(item for item in json.loads(before)["assembly"]["occurrences"] if item["id"] == "moved")["transform"]

    restarted = DocumentService(path, warmup=False)
    query = restarted.query({"select": {"kind": "assembly"}})
    transform_after = next(item for item in query["occurrences"] if item["occurrenceId"] == "moved")["transform"]
    assert path.read_bytes() == before
    assert transform_after["translation"]["value"] == transform_before["translation"]
    inspection = restarted.check({"action": "evaluate", "requirements": [{"id": "gap", "kind": "assembly_interference", "occurrenceIds": ["fixed", "moved"], "minimumClearance": {"value": 5, "unit": "mm"}}]})
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
    assert service.query({"select": {"kind": "assembly"}})["redundantMateIds"] == [
        "box-seat-2"
    ]


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
    roles = service.query({"select": {"kind": "face_roles", "id": "plate_extrude"}})[
        "roles"
    ]
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
