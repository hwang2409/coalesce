import json
import math
import re
import shutil
from pathlib import Path

import pytest
from build123d import Face, Shape
from OCP.BRepAlgoAPI import BRepAlgoAPI_Common
from OCP.BRepCheck import BRepCheck_Analyzer
from test_pcb_enclosure import (
    FEATURE_HOLES,
    FIXTURES,
    base_request,
    import_request,
    quantity,
    standoffs_request,
)
from test_pcb_lid import lid_request

from coalesce.references import Cardinality, Reference, Status
from coalesce.service import DocumentService


def port_request(
    *,
    revision=4,
    request_id="port",
    footprint="J1",
    wall="auto",
    envelope=None,
    **changes,
):
    parameters = {
        "base": {"featureId": "base"},
        "footprint": footprint,
        "wall": wall,
        "clearance": quantity(0.5),
        "envelope": envelope
        or {
            "source": "courtyard",
            "height": quantity(2),
            "bottomOffset": quantity(1),
        },
        **changes,
    }
    return {
        "action": "create",
        "expectedRevision": revision,
        "requestId": request_id,
        "featureId": "port",
        "kind": "port_cutout",
        "parameters": parameters,
    }


def explicit_envelope(width=2, height=2, bottom=1):
    return {
        "source": "explicit",
        "width": quantity(width),
        "height": quantity(height),
        "bottomOffset": quantity(bottom),
    }


def _service(tmp_path: Path, *, source: Path | None = None, lid=True):
    service = DocumentService(tmp_path / "port.coalesce.json")
    service.import_board(import_request(source or FIXTURES / "features.kicad_pcb"))
    service.feature(base_request())
    service.feature(standoffs_request())
    if lid:
        service.feature(lid_request())
    return service


def _faces(result, feature, role, source=None):
    return [
        item.entity
        for item in result.provenance.get(feature, [])
        if item.key.role == role
        and (source is None or item.key.source == source)
        and isinstance(item.entity, Face)
    ]


def _planar_face_area(result, role, axis, value):
    matches = []
    for face in _faces(result, "base", role):
        box = face.bounding_box()
        low = getattr(box.min, axis)
        high = getattr(box.max, axis)
        if low == pytest.approx(value, abs=1e-7) and high == pytest.approx(value, abs=1e-7):
            matches.append(face.area)
    assert len(matches) == 1
    return matches[0]


def _common_volume(first, second):
    common = BRepAlgoAPI_Common(first.wrapped, second.wrapped)
    common.Build()
    assert common.IsDone()
    return common.Shape().ShapeType(), sum(
        solid.volume for solid in Shape.cast(common.Shape()).solids()
    )


def test_courtyard_port_has_exact_opening_and_only_cuts_nearest_wall(tmp_path):
    service = _service(tmp_path, lid=False)
    before = service._regen
    unchanged = {
        ("X", 23): _planar_face_area(before, "outer_wall", "X", 23),
        ("Y", -3): _planar_face_area(before, "outer_wall", "Y", -3),
        ("Y", 13): _planar_face_area(before, "outer_wall", "Y", 13),
    }
    standoff_volume = before.shapes["mounts"].volume

    created = service.feature(port_request(revision=3))

    assert created["ok"] is True
    assert created["portCutout"] == {
        "footprintId": "front-id",
        "reference": "J1",
        "wall": "left",
        "envelopeSource": "courtyard",
        "opening": {
            "width": {"value": 5, "unit": "mm"},
            "height": {"value": 3, "unit": "mm"},
            "bottom": {"value": 1.7, "unit": "mm"},
            "top": {"value": 4.7, "unit": "mm"},
        },
        "cutsBelowBoardTop": False,
    }
    port = service._regen.shapes["port"]
    assert BRepCheck_Analyzer(port.wrapped).IsValid()
    assert len(port.solids()) == 1
    [top] = _faces(service._regen, "port", "port_top", "front-id")
    [bottom] = _faces(service._regen, "port", "port_bottom", "front-id")
    assert (top.bounding_box().min.Y, top.bounding_box().max.Y) == pytest.approx((2.5, 7.5))
    assert top.bounding_box().min.Z == pytest.approx(4.7)
    assert bottom.bounding_box().max.Z == pytest.approx(1.7)
    assert top.area == pytest.approx(10)
    assert bottom.area == pytest.approx(10)
    assert _planar_face_area(service._regen, "outer_wall", "X", -3) == pytest.approx(
        _planar_face_area(before, "outer_wall", "X", -3) - 15
    )
    for (axis, value), area in unchanged.items():
        assert _planar_face_area(service._regen, "outer_wall", axis, value) == pytest.approx(area)
    assert port.volume == pytest.approx(standoff_volume - 30)
    assert all(
        face.area == pytest.approx(math.pi * (2.5**2 - 0.8**2))
        for face in _faces(service._regen, "mounts", "standoff_top")
    )


def test_refresh_moves_port_and_deleted_footprint_fails_atomically_with_hint(tmp_path):
    source = tmp_path / "board.kicad_pcb"
    shutil.copyfile(FIXTURES / "features.kicad_pcb", source)
    service = _service(tmp_path, source=source, lid=False)
    assert service.feature(port_request(revision=3))["ok"] is True
    [original] = _faces(service._regen, "port", "port_top", "front-id")

    source.write_text(source.read_text().replace('(uuid "front-id") (at 1 5)', '(uuid "front-id") (at 1 6)'))
    moved = service.import_board(
        import_request(source, revision=4, request_id="move-port", action="refresh")
    )
    assert moved["ok"] is True
    [refreshed] = _faces(service._regen, "port", "port_top", "front-id")
    assert refreshed.bounding_box().min.Y - original.bounding_box().min.Y == pytest.approx(1)

    text = source.read_text()
    source.write_text(re.sub(r' \(footprint "Connector:Front".*?front\.wrl"\)\)\n', "", text, flags=re.DOTALL))
    before = service.store.path.read_bytes()
    missing = service.import_board(
        import_request(source, revision=5, request_id="delete-port", action="refresh")
    )
    assert missing["ok"] is False
    assert missing["error"]["code"] == "MISSING"
    assert missing["error"]["parameter"] == "footprint"
    assert missing["error"]["footprint"] == "front-id"
    assert "restore" in missing["error"]["hint"]
    assert service.revision == 5
    assert service.store.path.read_bytes() == before


def test_ambiguous_wall_missing_envelope_and_reference_are_structured_and_atomic(tmp_path):
    service = _service(tmp_path, lid=False)
    document = service.store.path
    before = document.read_bytes()

    ambiguous_wall = service.feature(
        port_request(
            revision=3,
            footprint="J3",
            envelope=explicit_envelope(),
        )
    )
    assert ambiguous_wall["ok"] is False
    assert ambiguous_wall["error"]["code"] == "AMBIGUOUS"
    assert ambiguous_wall["error"]["parameter"] == "wall"
    assert ambiguous_wall["error"]["candidates"] == ["back", "front"]

    missing_envelope = service.feature(
        port_request(revision=3, request_id="missing-envelope", footprint="J2")
    )
    assert missing_envelope["ok"] is False
    assert missing_envelope["error"]["code"] == "MISSING"
    assert missing_envelope["error"]["parameter"] == "envelope"
    assert missing_envelope["error"]["footprint"] == "back-id"
    assert "explicit" in missing_envelope["error"]["hint"]

    ambiguous_reference = service.feature(
        port_request(
            revision=3,
            request_id="ambiguous-reference",
            footprint="REF**",
            wall="left",
            envelope=explicit_envelope(),
        )
    )
    assert ambiguous_reference["ok"] is False
    assert ambiguous_reference["error"]["code"] == "AMBIGUOUS"
    assert ambiguous_reference["error"]["parameter"] == "footprint"
    assert ambiguous_reference["error"]["candidates"] == ["m2-id", "m3-id"]
    assert service.revision == 3
    assert document.read_bytes() == before


def test_reference_resolution_update_and_restart_preserve_port_lineage(tmp_path):
    service = _service(tmp_path, lid=False)
    assert service.feature(port_request(revision=3, footprint="front-id", wall="left"))["ok"]

    updated = service.feature(
        {
            "action": "update",
            "expectedRevision": 4,
            "requestId": "wider-clearance",
            "featureId": "port",
            "kind": "port_cutout",
            "set": {"clearance": quantity(0.75)},
        }
    )

    assert updated["ok"] is True
    resolution = service._regen.resolve(
        Reference("port", "port_top", Cardinality.ONE, source="front-id")
    )
    assert resolution.status is Status.OK
    assert resolution.error is None
    [top] = resolution.candidates
    assert top.entity.bounding_box().max.Y - top.entity.bounding_box().min.Y == pytest.approx(5.5)

    restarted = DocumentService(service.store.path)
    graph = restarted.query({"select": {"kind": "graph"}})["graph"]
    assert [node["type"] for node in graph["nodes"]] == [
        "BoardImport",
        "PcbBase",
        "Standoffs",
        "PortCutout",
    ]
    restarted_resolution = restarted._regen.resolve(
        Reference("port", "port_top", Cardinality.ONE, source="front-id")
    )
    assert restarted_resolution.status is Status.OK
    assert restarted._regen.shapes["port"].volume == pytest.approx(
        service._regen.shapes["port"].volume
    )


def _ordered_modifiers(tmp_path, order):
    service = DocumentService(tmp_path / f"{'-'.join(order)}.coalesce.json")
    assert service.import_board(import_request(FIXTURES / "features.kicad_pcb"))["ok"]
    assert service.feature(base_request())["ok"]
    for revision, feature in enumerate(order, start=2):
        request = (
            port_request(
                revision=revision,
                footprint="center-id",
                wall="back",
                envelope=explicit_envelope(),
            )
            if feature == "port"
            else standoffs_request(revision=revision)
        )
        assert service.feature(request)["ok"]
    return service


def _terminal_shape(service):
    [terminal] = service.query({"select": {"kind": "measurements"}})["parts"]
    return service._regen.shapes[terminal["terminalFeatureId"]]


def _body_signature(service):
    shape = _terminal_shape(service)
    roles = sorted(
        item.key.role
        for candidates in service._regen.provenance.values()
        for item in candidates
        if item.key.feature in {"base", "mounts", "port"}
    )
    return (
        shape.volume,
        sorted(face.area for face in shape.faces()),
        roles,
    )


def test_legacy_port_target_document_loads_into_base_lineage(tmp_path):
    service = _service(tmp_path, lid=False)
    assert service.feature(port_request(revision=3))["ok"]
    document = json.loads(service.store.path.read_text())
    [port] = [node for node in document["graph"]["nodes"] if node["type"] == "PortCutout"]
    port["target"] = "mounts"
    service.store.path.write_text(json.dumps(document))

    restarted = DocumentService(service.store.path)
    graph = restarted.query({"select": {"kind": "graph"}})["graph"]
    assert restarted._regen.shapes["port"].volume == pytest.approx(
        service._regen.shapes["port"].volume
    )
    [stored_port] = [
        node
        for node in graph["nodes"]
        if node["type"] == "PortCutout"
    ]
    assert "target" not in stored_port


def test_standoffs_and_port_compose_identically_in_both_creation_orders(tmp_path):
    port_first = _ordered_modifiers(tmp_path, ("port", "mounts"))
    standoffs_first = _ordered_modifiers(tmp_path, ("mounts", "port"))

    first_volume, first_areas, first_roles = _body_signature(port_first)
    second_volume, second_areas, second_roles = _body_signature(standoffs_first)
    assert first_volume == pytest.approx(second_volume, abs=1e-8)
    assert first_areas == pytest.approx(second_areas, abs=1e-8)
    assert first_roles == second_roles


def test_port_first_still_reports_standoff_collision_atomically(tmp_path):
    service = DocumentService(tmp_path / "collision.coalesce.json")
    assert service.import_board(import_request(FIXTURES / "features.kicad_pcb"))["ok"]
    assert service.feature(base_request())["ok"]
    assert service.feature(
        port_request(revision=2, envelope=explicit_envelope(width=4, bottom=-1))
    )["ok"]
    before = service.store.path.read_bytes()

    failed = service.feature(standoffs_request(revision=3))

    assert failed["ok"] is False
    assert failed["error"]["code"] == "INVALID_PARAMETER"
    assert failed["error"]["feature"] == "port"
    assert failed["error"]["conflict"] in FEATURE_HOLES
    assert service.revision == 3
    assert service.store.path.read_bytes() == before


def _lid_conflict_service(tmp_path, order, *, port_changes=None, lid_changes=None):
    service = DocumentService(tmp_path / f"lid-{'-'.join(order)}.coalesce.json")
    assert service.import_board(import_request(FIXTURES / "features.kicad_pcb"))["ok"]
    assert service.feature(base_request())["ok"]
    results = []
    for revision, feature in enumerate(order, start=2):
        request = (
            port_request(revision=revision, **(port_changes or {}))
            if feature == "port"
            else lid_request(revision=revision, **(lid_changes or {}))
        )
        results.append(service.feature(request))
    return service, results


@pytest.mark.parametrize("order", [("port", "lid"), ("lid", "port")])
def test_lid_lip_blocking_opening_is_structured_and_atomic_in_both_orders(tmp_path, order):
    service, results = _lid_conflict_service(tmp_path, order)

    assert results[0]["ok"] is True
    failed = results[1]
    assert failed["ok"] is False
    assert failed["error"]["code"] == "PORT_BLOCKED_BY_LID"
    assert failed["error"]["port"] == "port"
    assert failed["error"]["lid"] == "lid"
    assert failed["error"]["overlapVolume"] == pytest.approx(9.375)
    assert failed["error"]["overlapBounds"] == {
        "min": pytest.approx([-0.75, 2.5, 2.2]),
        "max": pytest.approx([0, 7.5, 4.7]),
        "unit": "mm",
    }
    assert any("height" in hint for hint in failed["error"]["hints"])
    assert any("bottomOffset" in hint for hint in failed["error"]["hints"])
    assert any("lip" in hint or "lid" in hint for hint in failed["error"]["hints"])
    assert service.revision == 3


@pytest.mark.parametrize("order", [("port", "lid"), ("lid", "port")])
def test_opening_reaching_lid_slab_at_rim_is_rejected_in_both_orders(tmp_path, order):
    _, results = _lid_conflict_service(
        tmp_path,
        order,
        port_changes={"envelope": explicit_envelope(height=3)},
        lid_changes={"include_lip": False},
    )

    assert results[0]["ok"] is True
    assert results[1]["ok"] is False
    assert results[1]["error"]["code"] == "PORT_BLOCKED_BY_LID"
    assert results[1]["error"]["overlapVolume"] > 0


def test_non_overlapping_opening_with_lid_succeeds(tmp_path):
    service, results = _lid_conflict_service(
        tmp_path,
        ("lid", "port"),
        port_changes={"envelope": explicit_envelope(height=2)},
        lid_changes={"include_lip": False},
    )

    assert all(result["ok"] for result in results)
    assert _common_volume(service._regen.opening_tools["port"], service._regen.shapes["lid"])[1] == pytest.approx(
        0, abs=1e-8
    )


def test_lowering_blocked_opening_repairs_lid_conflict(tmp_path):
    service, results = _lid_conflict_service(tmp_path, ("lid", "port"))
    assert results[1]["error"]["code"] == "PORT_BLOCKED_BY_LID"

    repaired = service.feature(
        port_request(
            revision=3,
            request_id="lower-port",
            clearance=quantity(0.1),
            envelope=explicit_envelope(height=0.4, bottom=0.1),
        )
    )

    assert repaired["ok"] is True
    assert service._regen.metadata["port"]["opening"]["top"]["value"] < 2.2
