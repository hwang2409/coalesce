import json
import math
import subprocess
import sys
from dataclasses import replace

import pytest
from build123d import Face, GeomType
from test_pcb_enclosure import FIXTURES, base_request, import_request, quantity
from test_pcb_lid import lid_request
from test_port_cutout import explicit_envelope, port_request

from coalesce.lid_outline_compare import (
    LidOutlineGeometry,
    LidOutlineSegment,
    compare_lid_outlines,
)
from coalesce.service import DocumentService
from coalesce.viewer import ViewerSceneStore


def _fillet_request(*, revision: int, radius: float = 1) -> dict:
    return {
        "action": "create",
        "expectedRevision": revision,
        "requestId": "round-base",
        "featureId": "base_round",
        "kind": "fillet",
        "parameters": {
            "target": {"featureId": "base"},
            "edges": [
                {
                    "feature": "base",
                    "role": "outer_vertical",
                    "cardinality": "exactly",
                    "count": 4,
                }
            ],
            "radius": quantity(radius),
        },
    }


def _update_fillet(*, revision: int, radius: float) -> dict:
    return {
        "action": "update",
        "expectedRevision": revision,
        "requestId": f"round-base-{radius}",
        "featureId": "base_round",
        "kind": "fillet",
        "set": {"radius": quantity(radius)},
    }


def _update_lid_source(*, revision: int, source: str = "base_round") -> dict:
    return {
        "action": "update",
        "expectedRevision": revision,
        "requestId": f"lid-source-{revision}",
        "featureId": "lid",
        "kind": "pcb_lid",
        "set": {"outlineSource": {"featureId": source}},
    }


def _service(tmp_path, name="outline") -> DocumentService:
    service = DocumentService(tmp_path / f"{name}.coalesce.json")
    assert service.import_board(import_request(FIXTURES / "features.kicad_pcb"))["ok"]
    assert service.feature(base_request())["ok"]
    return service


def _outline_measurements(service: DocumentService) -> tuple[float, tuple[float, ...], list]:
    if service._regen is None:
        service.query({"select": {"kind": "graph"}})
    [top] = [
        item.entity
        for item in service._regen.provenance["lid"]
        if item.key.role == "top" and isinstance(item.entity, Face)
    ]
    box = top.bounding_box()
    cylinders = [
        item.entity.radius
        for item in service._regen.provenance["lid"]
        if item.key.role == "edge"
        and isinstance(item.entity, Face)
        and item.entity.geom_type is GeomType.CYLINDER
    ]
    return top.area, (box.min.X, box.min.Y, box.max.X, box.max.Y), cylinders


def _check_outline(service: DocumentService) -> dict:
    response = service.check(
        {
            "action": "evaluate",
            "requirements": [{"id": "outline", "kind": "lid_outline_match"}],
        }
    )
    assert response["ok"] is True
    return response["results"][0]


def test_lid_uses_filleted_terminal_outline_with_analytic_arcs(tmp_path):
    service = _service(tmp_path)
    assert service.feature(_fillet_request(revision=2))["ok"]
    assert service.feature(lid_request(revision=3, include_lip=False))["ok"]

    area, bbox, cylinders = _outline_measurements(service)
    result = _check_outline(service)

    assert result["status"] == "PASS"
    assert result["measured"]["areaDifference"]["value"] == pytest.approx(0, abs=1e-6)
    assert result["measured"]["bboxDifference"]["value"] == pytest.approx(0, abs=1e-6)
    assert area == pytest.approx(412 + math.pi, abs=1e-6)
    assert bbox == pytest.approx((-3, -3, 23, 13), abs=1e-6)
    assert len(cylinders) == 4
    assert cylinders == pytest.approx([1, 1, 1, 1], abs=1e-6)
    assert service._document.graph.nodes["lid"].outline_source == "base_round"
    ordered = [node.id for node in service._document.graph.ordered()]
    assert ordered.index("base_round") < ordered.index("lid")


def test_changed_source_geometry_updates_lid_without_rebinding(tmp_path):
    service = _service(tmp_path)
    assert service.feature(_fillet_request(revision=2))["ok"]
    assert service.feature(lid_request(revision=3, include_lip=False))["ok"]

    assert service.feature(_update_fillet(revision=4, radius=2))["ok"]
    inspection = service.query({"select": {"kind": "enclosure", "base": "base"}})

    area, bbox, cylinders = _outline_measurements(service)
    assert area == pytest.approx(400 + 4 * math.pi, abs=1e-6)
    assert bbox == pytest.approx((-3, -3, 23, 13), abs=1e-6)
    assert cylinders == pytest.approx([2, 2, 2, 2], abs=1e-6)
    assert not any(item["code"] == "LID_OUTLINE_STALE" for item in inspection["warnings"])
    assert _check_outline(service)["status"] == "PASS"


def test_board_refresh_updates_lid_outline(tmp_path):
    source = tmp_path / "wider.kicad_pcb"
    source.write_text((FIXTURES / "features.kicad_pcb").read_text())
    service = DocumentService(tmp_path / "refresh.coalesce.json")
    assert service.import_board(import_request(source))["ok"]
    assert service.feature(base_request())["ok"]
    assert service.feature(_fillet_request(revision=2))["ok"]
    assert service.feature(lid_request(revision=3, include_lip=False))["ok"]

    source.write_text(source.read_text().replace("(end 20 10)", "(end 22 10)"))
    refreshed = service.import_board({
        **import_request(source, revision=4, request_id="refresh", action="refresh"),
        "path": str(source.resolve()),
    })

    assert refreshed["ok"] is True
    assert refreshed["boardChanges"]["outlineChanged"] is True
    assert refreshed["boardChanges"]["dependentFeatures"] == ["pcb base", "fillet", "pcb lid"]
    _, bbox, _ = _outline_measurements(service)
    assert bbox == pytest.approx((-3, -3, 25, 13), abs=1e-6)
    assert _check_outline(service)["status"] == "PASS"


def _lid_chamfer_request(*, revision: int, count: int = 4) -> dict:
    return {
        "action": "create",
        "expectedRevision": revision,
        "requestId": "chamfer-lid",
        "featureId": "lid_chamfer",
        "kind": "chamfer",
        "parameters": {
            "target": {"featureId": "lid"},
            "edges": [
                {
                    "feature": "lid",
                    "role": "between(edge,top)",
                    "cardinality": "exactly",
                    "count": count,
                }
            ],
            "distance": quantity(0.5),
        },
    }


def _refresh_with_lid_chamfer(tmp_path, name: str, *, fillet: bool = False):
    source = tmp_path / f"{name}.kicad_pcb"
    source.write_text((FIXTURES / "features.kicad_pcb").read_text())
    store = ViewerSceneStore()
    service = DocumentService(tmp_path / f"{name}.coalesce.json", scene_store=store)
    assert service.import_board(import_request(source))["ok"]
    assert service.feature(base_request())["ok"]
    revision = 2
    if fillet:
        assert service.feature(_fillet_request(revision=revision))["ok"]
        revision += 1
    else:
        assert service.feature(
            port_request(
                revision=revision,
                envelope=explicit_envelope(width=4, height=1, bottom=0),
            )
        )["ok"]
        revision += 1
    assert service.feature(lid_request(revision=revision, include_lip=False))["ok"]
    revision += 1
    assert service.feature(
        _lid_chamfer_request(revision=revision, count=8 if fillet else 4)
    )["ok"]
    return service, source, revision + 1


def _refresh_board(service: DocumentService, source, revision: int, request_id: str) -> dict:
    return service.import_board({
        **import_request(source, revision=revision, request_id=request_id, action="refresh"),
        "path": str(source.resolve()),
    })


def _notched_outline(left: int, right: int) -> str:
    return (
        "(gr_poly (pts (xy 0 0) (xy 20 0) (xy 20 10) "
        f"(xy {right} 10) (xy {right} 8) (xy {left} 8) (xy {left} 10) (xy 0 10)) "
        '(stroke (width 0.05) (type default)) (fill none) (layer "Edge.Cuts") '
        '(uuid "outline"))'
    )


def test_equal_bounds_moved_notch_reports_lid_and_chamfer(tmp_path):
    source = tmp_path / "notched.kicad_pcb"
    original = _notched_outline(6, 14)
    source.write_text(
        (FIXTURES / "features.kicad_pcb").read_text().replace(
            '(gr_rect (start 0 0) (end 20 10) (layer "Edge.Cuts") (uuid "rect"))',
            original,
        )
    )
    service = DocumentService(tmp_path / "notched.coalesce.json")
    assert service.import_board(import_request(source))["ok"]
    assert service.feature(base_request())["ok"]
    assert service.feature(lid_request(revision=2, include_lip=False))["ok"]
    assert service.feature(_lid_chamfer_request(revision=3, count=8))["ok"]
    source.write_text(source.read_text().replace(original, _notched_outline(7, 15)))

    refreshed = _refresh_board(service, source, 4, "move-notch")

    assert refreshed["boardChanges"]["outlineBounds"] == {
        "before": {"width": 20.0, "height": 10.0},
        "after": {"width": 20.0, "height": 10.0},
    }
    assert refreshed["boardChanges"]["dependentFeatures"] == [
        "pcb base",
        "pcb lid",
        "chamfer",
    ]


def test_refresh_reports_lid_and_chamfer_only_when_lid_outline_changes(tmp_path):
    service, source, revision = _refresh_with_lid_chamfer(tmp_path, "j1")
    source.write_text(source.read_text().replace("(at 1 5)", "(at 1 6)"))

    j1 = _refresh_board(service, source, revision, "move-j1")

    assert j1["boardChanges"]["dependentFeatures"] == ["port cutout"]

    wider_service, wider_source, wider_revision = _refresh_with_lid_chamfer(tmp_path, "wider")
    wider_source.write_text(wider_source.read_text().replace("(end 20 10)", "(end 22 10)"))

    wider = _refresh_board(wider_service, wider_source, wider_revision, "widen-board")

    assert "pcb lid" in wider["boardChanges"]["dependentFeatures"]
    assert "chamfer" in wider["boardChanges"]["dependentFeatures"]

    rounded, rounded_source, rounded_revision = _refresh_with_lid_chamfer(
        tmp_path, "rounded", fillet=True
    )
    rounded_source.write_text(
        rounded_source.read_text().replace("(end 20 10)", "(end 22 10)")
    )

    changed_outline = _refresh_board(
        rounded, rounded_source, rounded_revision, "change-rounded-outline"
    )

    assert "fillet" in changed_outline["boardChanges"]["dependentFeatures"]
    assert "pcb lid" in changed_outline["boardChanges"]["dependentFeatures"]
    assert "chamfer" in changed_outline["boardChanges"]["dependentFeatures"]


def test_outline_source_survives_reload_and_separate_export(tmp_path):
    service = _service(tmp_path)
    assert service.feature(_fillet_request(revision=2))["ok"]
    assert service.feature(lid_request(revision=3, include_lip=False))["ok"]
    document = service.store.path

    restarted = DocumentService(document)
    assert restarted._document.graph.nodes["lid"].outline_source == "base_round"
    assert _check_outline(restarted)["status"] == "PASS"

    destination = tmp_path / "delivery"
    exported = restarted.export(
        {
            "action": "publish",
            "mode": "separate",
            "expectedRevision": 4,
            "path": str(destination),
            "base": "base",
            "requirements": [{"id": "outline", "kind": "lid_outline_match"}],
        }
    )
    assert exported["ok"] is True
    assert exported["ready"] is True
    assert sorted(path.name for path in destination.glob("*.step")) == ["base.step", "lid.step"]
    serialized = json.loads(document.read_text())
    lid = next(node for node in serialized["graph"]["nodes"] if node["id"] == "lid")
    assert lid["outlineSource"]["featureId"] == "base_round"
    assert "outlineGeometry" not in lid


def test_low_port_after_lid_fails_outline_match_as_stale(tmp_path):
    service = _service(tmp_path)
    assert service.feature(lid_request(revision=2, include_lip=False))["ok"]
    assert service.feature(
        port_request(
            revision=3,
            envelope=explicit_envelope(width=4, height=1, bottom=0),
        )
    )["ok"]

    result = _check_outline(service)

    assert result["status"] == "FAIL"
    assert "stale" in result["reason"]


def _line(start: tuple[float, float], end: tuple[float, float]) -> LidOutlineSegment:
    return LidOutlineSegment(
        kind="line",
        start=(*start, 0),
        midpoint=((start[0] + end[0]) / 2, (start[1] + end[1]) / 2, 0),
        end=(*end, 0),
        source="test",
    )


def _rectangle(*, split_top: bool = False) -> LidOutlineGeometry:
    points = [(0, 0), (2, 0), (2, 1), (0, 1)]
    if split_top:
        points.insert(3, (1, 1))
    return LidOutlineGeometry(
        tuple(_line(start, points[(index + 1) % len(points)]) for index, start in enumerate(points)),
        area=2,
        bbox=(0, 0, 2, 1),
    )


def test_outline_comparison_rejects_equal_area_bbox_with_different_topology():
    result = compare_lid_outlines("base", "base", _rectangle(), _rectangle(split_top=True))

    assert result.matches is False
    assert result.reason == "segment count differs: 4 != 5"
    assert result.area_difference == pytest.approx(0, abs=1e-6)
    assert result.bbox_difference == pytest.approx(0, abs=1e-6)


def test_outline_comparison_rejects_different_segment_kind():
    source = _rectangle()
    first = replace(source.segments[0], center=(1, 0, 0), radius=1)
    source = replace(source, segments=(first, *source.segments[1:]))
    current = replace(source, segments=(replace(first, kind="arc"), *source.segments[1:]))

    result = compare_lid_outlines("base", "base", source, current)

    assert result.matches is False
    assert result.reason == "segment kind differs"


def test_outline_comparison_rejects_different_arc_radius():
    source = _rectangle()
    arc = replace(source.segments[0], kind="arc", center=(1, 0, 0), radius=1)
    source = replace(source, segments=(arc, *source.segments[1:]))
    current = replace(source, segments=(replace(arc, radius=2), *source.segments[1:]))

    result = compare_lid_outlines("base", "base", source, current)

    assert result.matches is False
    assert result.reason == "arc radius differs"


def test_outline_comparison_accepts_matching_geometry():
    result = compare_lid_outlines("base", "base", _rectangle(), _rectangle())

    assert result.matches is True
    assert result.reason is None


def test_importing_outline_comparison_does_not_import_build123d():
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import coalesce.lid_outline_compare; "
                "assert 'build123d' not in sys.modules"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert probe.returncode == 0, probe.stderr


def test_treatment_after_lid_reports_stale_until_update(tmp_path):
    service = _service(tmp_path)
    assert service.feature(lid_request(revision=2, include_lip=False))["ok"]
    assert service.feature(_fillet_request(revision=3))["ok"]

    inspection = service.query({"select": {"kind": "enclosure", "base": "base"}})
    assert any(item["code"] == "LID_OUTLINE_STALE" for item in inspection["warnings"])
    assert _check_outline(service)["status"] == "FAIL"

    assert service.feature(_update_lid_source(revision=4))["ok"]
    assert not any(
        item["code"] == "LID_OUTLINE_STALE"
        for item in service.query({"select": {"kind": "enclosure", "base": "base"}})["warnings"]
    )
    assert _check_outline(service)["status"] == "PASS"


def test_broken_rim_source_fails_atomically(tmp_path):
    service = _service(tmp_path)
    assert service.feature(
        port_request(
            revision=2,
            envelope=explicit_envelope(width=4, height=4, bottom=2),
        )
    )["ok"]
    revision = service.revision
    before = service.store.path.read_bytes()

    failed = service.feature(lid_request(revision=revision, include_lip=False))

    assert failed["ok"] is False
    assert failed["error"]["code"] in {"EMPTY", "SPLIT", "AMBIGUOUS"}
    assert service.revision == revision
    assert service.store.path.read_bytes() == before


def test_legacy_lid_without_outline_source_keeps_board_outline(tmp_path):
    service = _service(tmp_path)
    assert service.feature(lid_request(revision=2, include_lip=False))["ok"]
    original = _outline_measurements(service)
    payload = json.loads(service.store.path.read_text())
    lid = next(node for node in payload["graph"]["nodes"] if node["id"] == "lid")
    lid.pop("outlineSource", None)
    service.store.path.write_text(json.dumps(payload))

    restarted = DocumentService(service.store.path)

    assert restarted._document.graph.nodes["lid"].outline_source is None
    assert _outline_measurements(restarted) == original
    check = _check_outline(restarted)
    assert check["status"] == "PASS"
    assert check["measured"]["matches"] is True
    assert check["measured"]["areaDifference"]["value"] == 0
    assert check["measured"]["bboxDifference"]["value"] == 0
    assert "reason" not in check
    assert not any(
        warning["code"] == "LID_OUTLINE_STALE"
        for warning in restarted.query({"select": {"kind": "enclosure", "base": "base"}})["warnings"]
    )
