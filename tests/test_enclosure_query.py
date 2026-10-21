import json
import shutil

import pytest
from build123d import Align, Box, Compound, Edge, Face, Location, Shape
from jsonschema import validate
from OCP.BRepAlgoAPI import BRepAlgoAPI_Common
from OCP.BRepExtrema import BRepExtrema_DistShapeShape
from test_mcp_cut_hole_circle import cut, rectangle_sketch
from test_mcp_truthful_contract import list_server_tools
from test_mcp_vertical import run_server
from test_pcb_enclosure import (
    FEATURE_HOLES,
    FIXTURES,
    base_request,
    import_request,
    standoffs_request,
)
from test_pcb_lid import lid_request
from test_port_cutout import port_request

from coalesce.enclosure_inspection import inspect_enclosure
from coalesce.graph import Chamfer, Fillet, GraphError
from coalesce.pcb import board_loop_wire
from coalesce.references import Cardinality, Position, Reference
from coalesce.regen import regenerate
from coalesce.serialization import dumps, loads
from coalesce.service import DocumentService

UNUSED_HOLE = "hole-9ddb205ba1bc297f95347452"


def _value(quantity):
    assert quantity["unit"] == "mm"
    return quantity["value"]


def _bbox(shape):
    box = shape.bounding_box()
    return {
        "min": (box.min.X, box.min.Y, box.min.Z),
        "max": (box.max.X, box.max.Y, box.max.Z),
    }


def _faces(service, feature, role):
    return [
        item.entity
        for item in service._regen.provenance[feature]
        if item.key.role == role and isinstance(item.entity, Face)
    ]


def _common_volume(first, second):
    common = BRepAlgoAPI_Common(first.wrapped, second.wrapped)
    common.Build()
    assert common.IsDone()
    return Shape.cast(common.Shape()).volume


def _exact_distance(first, second):
    extrema = BRepExtrema_DistShapeShape(first.wrapped, second.wrapped)
    extrema.Perform()
    assert extrema.IsDone()
    return extrema.Value()


def _filleted_inspection(service, filleted_role):
    graph = loads(dumps(service._document.graph))
    graph.add(
        Fillet(
            "wall_round",
            target="base",
            edges=[
                Reference(
                    "base",
                    f"between({filleted_role},rim)",
                    Cardinality.ONE_OR_MORE,
                )
            ],
            radius=0.25,
        )
    )
    regenerated = regenerate(graph)
    blend = Compound(
        children=[
            candidate.entity
            for candidate in regenerated.provenance["wall_round"]
            if candidate.key.role.startswith("blend(")
        ]
    )
    opposite_role = "outer_wall" if filleted_role == "inner_wall" else "inner_wall"
    opposite = Compound(
        children=[
            candidate.entity
            for candidate in regenerated.provenance["base"]
            if candidate.key.role == opposite_role
        ]
    )
    return inspect_enclosure(graph, regenerated, "base"), _exact_distance(blend, opposite)


def _edge_reference(candidate):
    center = candidate.descriptor["center"]
    return Reference(
        candidate.key.feature,
        candidate.key.role,
        source=candidate.key.source,
        body=candidate.key.body,
        filters=[
            Position(direction=direction, value=center[index])
            for index, direction in enumerate(((1, 0, 0), (0, 1, 0), (0, 0, 1)))
        ],
    )


def _terminal_edge_candidates(regenerated, terminal):
    final_edges = regenerated.shapes[terminal].edges()
    candidates = [
        candidate
        for items in regenerated.provenance.values()
        for candidate in items
        if isinstance(candidate.entity, Edge)
        and any(candidate.entity.is_same(edge) for edge in final_edges)
    ]
    unique = []
    for candidate in candidates:
        if not any(candidate.entity.is_same(existing.entity) for existing in unique):
            unique.append(candidate)
    return unique


def _boundary_faces(regenerated, fillet_id, edge_role, kind):
    role = {"wall": ("inner_wall", "outer_wall"), "floor": ("floor", "floor_bottom")}
    inside_role, outside_role = role[kind]
    inside = [
        candidate.entity
        for candidate in regenerated.provenance["base"]
        if candidate.key.role == inside_role
    ]
    outside = [
        candidate.entity
        for candidate in regenerated.provenance["base"]
        if candidate.key.role == outside_role
    ]
    blends = [
        candidate.entity
        for candidate in regenerated.provenance[fillet_id]
        if candidate.key.role.startswith(("blend(", "chamfer("))
    ]
    edge_roles = edge_role.removeprefix("between(").removesuffix(")").split(",")
    if inside_role in edge_roles:
        inside.extend(blends)
    if outside_role in edge_roles:
        outside.extend(blends)
    return Compound(children=inside), Compound(children=outside)


def _independent_thicknesses(regenerated, fillet_id, edge_role):
    return {
        kind: _exact_distance(*_boundary_faces(regenerated, fillet_id, edge_role, kind))
        for kind in ("wall", "floor")
    }


def _thickness_outcome(result, truth):
    measured = {
        "wall": result["base"]["minimumWallThickness"],
        "floor": result["base"]["floorThickness"],
    }
    if any(value.get("status") == "unknown" for value in measured.values()):
        return "unknown"
    exact = all(
        measured[kind]["value"] == pytest.approx(truth[kind], abs=1e-6) for kind in truth
    )
    return "exact" if exact else "wrong"


def _fillet_case(graph, reference, fillet_id):
    graph = loads(dumps(graph))
    graph.add(Fillet(fillet_id, target="port", edges=[reference], radius=0.25))
    regenerated = regenerate(graph)
    result = inspect_enclosure(graph, regenerated, "base")
    truth = _independent_thicknesses(regenerated, fillet_id, reference.role)
    return _thickness_outcome(result, truth)


def _assert_bbox(actual, expected):
    assert actual["min"]["unit"] == actual["max"]["unit"] == "mm"
    assert tuple(actual["min"][axis] for axis in "xyz") == pytest.approx(expected["min"], abs=1e-6)
    assert tuple(actual["max"][axis] for axis in "xyz") == pytest.approx(expected["max"], abs=1e-6)


def _join(request_id, revision, feature_id, sketch_id, target, distance):
    return {
        "action": "create",
        "expectedRevision": revision,
        "requestId": request_id,
        "featureId": feature_id,
        "kind": "extrude",
        "parameters": {
            "profile": {"sketchId": sketch_id, "profileIds": [f"{sketch_id}_profile"]},
            "distance": {"value": distance, "unit": "mm"},
            "operation": "join",
            "target": {"featureId": target},
            "direction": "normal",
        },
    }


def _create_rectangle(
    service, *, revision, sketch_id, width, height, plane="XY", offset=0, center=(0, 0)
):
    return service.sketch(
        rectangle_sketch(
            request_id=sketch_id,
            revision=revision,
            sketch_id=sketch_id,
            entity_id=f"{sketch_id}_profile",
            width=width,
            height=height,
            plane=plane,
            offset=offset,
            center=center,
        )
    )


def _base_service(tmp_path, *, standoffs=False, lid=False, port=False):
    service = DocumentService(tmp_path / "as-built.coalesce.json")
    assert service.import_board(import_request(FIXTURES / "features.kicad_pcb"))["ok"]
    assert service.feature(base_request())["ok"]
    revision = 2
    terminal = "base"
    if standoffs:
        assert service.feature(standoffs_request())["ok"]
        revision += 1
        terminal = "mounts"
    if lid:
        assert service.feature(lid_request(revision=revision))["ok"]
        revision += 1
    if port:
        assert service.feature(port_request(revision=revision))["ok"]
        revision += 1
        terminal = "port"
    return service, revision, terminal


@pytest.mark.anyio
async def test_enclosure_query_measures_complete_fixture_through_public_mcp(tmp_path):
    document = tmp_path / "enclosure.coalesce.json"
    calls = [
        ("import", import_request(FIXTURES / "features.kicad_pcb")),
        ("feature", base_request(clearance={"value": 0.5, "unit": "mm"})),
        (
            "feature",
            {
                "action": "create",
                "expectedRevision": 2,
                "requestId": "standoffs",
                "featureId": "mounts",
                "kind": "standoffs",
                "parameters": {
                    "target": {"featureId": "base"},
                    "board": {"featureId": "controller_board"},
                    "selection": {"mode": "explicit", "holeIds": FEATURE_HOLES},
                    "bossDiameter": {"value": 4, "unit": "mm"},
                    "pilotDiameter": {"value": 1.6, "unit": "mm"},
                },
            },
        ),
        (
            "feature",
            lid_request(
                lip={"depth": {"value": 0.2, "unit": "mm"}, "width": {"value": 1, "unit": "mm"}}
            ),
        ),
        ("feature", port_request()),
        ("query", {"select": {"kind": "enclosure", "base": "base"}}),
    ]

    *_, result = await run_server(document, calls)
    service = DocumentService(document)
    assert service.query({"select": {"kind": "measurements"}})["ok"] is True
    board = service._regen.shapes["controller_board"]
    base = service._regen.shapes["port"]
    lid = service._regen.shapes["lid"]
    board_box = _bbox(board)
    base_box = _bbox(base)
    lid_box = _bbox(lid)

    assert result["ok"] is True
    assert result["revision"] == 5
    assert result["units"] == "mm"
    assert result["baseId"] == "base"
    assert result["board"]["assetId"] == "controller_board"
    _assert_bbox(result["board"]["outlineBBox"], board_box)
    assert _value(result["board"]["topZ"]) == pytest.approx(board_box["max"][2], abs=1e-6)

    _assert_bbox(result["base"]["outerBBox"], base_box)
    assert tuple(_value(result["base"]["outerSize"][axis]) for axis in "xyz") == pytest.approx(
        tuple(base_box["max"][i] - base_box["min"][i] for i in range(3)), abs=1e-6
    )
    wall = min(
        inner.distance_to(outer)
        for inner in _faces(service, "base", "inner_wall")
        for outer in _faces(service, "base", "outer_wall")
        if inner.distance_to(outer) > 1e-7
    )
    floor = min(
        top.distance_to(bottom)
        for top in _faces(service, "base", "floor")
        for bottom in _faces(service, "base", "floor_bottom")
    )
    assert _value(result["base"]["minimumWallThickness"]) == pytest.approx(wall, abs=1e-6)
    assert _value(result["base"]["floorThickness"]) == pytest.approx(floor, abs=1e-6)

    inner = Compound(children=_faces(service, "base", "inner_wall")).bounding_box()
    assert (
        _value(result["cavity"]["innerSize"]["x"]),
        _value(result["cavity"]["innerSize"]["y"]),
    ) == pytest.approx((inner.max.X - inner.min.X, inner.max.Y - inner.min.Y), abs=1e-6)
    assert {
        "left": _value(result["cavity"]["boardClearance"]["left"]),
        "right": _value(result["cavity"]["boardClearance"]["right"]),
        "front": _value(result["cavity"]["boardClearance"]["front"]),
        "back": _value(result["cavity"]["boardClearance"]["back"]),
    } == pytest.approx(
        {
            "left": board_box["min"][0] - inner.min.X,
            "right": inner.max.X - board_box["max"][0],
            "front": board_box["min"][1] - inner.min.Y,
            "back": inner.max.Y - board_box["max"][1],
        },
        abs=1e-6,
    )

    assert result["standoffs"]["count"] == 2
    assert result["standoffs"]["mountingHoleCount"] == 3
    assert result["standoffs"]["coveredHoleIds"] == FEATURE_HOLES
    assert result["standoffs"]["uncoveredHoleIds"] == [UNUSED_HOLE]
    assert [item["holeId"] for item in result["standoffs"]["items"]] == FEATURE_HOLES
    for item in result["standoffs"]["items"]:
        [side] = [
            candidate.entity
            for candidate in service._regen.provenance["mounts"]
            if candidate.key.role == "standoff_side" and candidate.key.source == item["holeId"]
        ]
        side_box = side.bounding_box()
        assert (item["axis"]["x"], item["axis"]["y"], item["axis"]["z"]) == pytest.approx(
            (
                (side_box.min.X + side_box.max.X) / 2,
                (side_box.min.Y + side_box.max.Y) / 2,
                side_box.min.Z,
            ),
            abs=1e-6,
        )
        assert item["axis"]["unit"] == "mm"
        assert item["classification"] == "intact"
        assert _value(item["designed"]["bossDiameter"]) == pytest.approx(
            side.radius * 2, abs=1e-6
        )
        assert _value(item["designed"]["pilotDiameter"]) == pytest.approx(1.6, abs=1e-6)
        assert _value(item["measured"]["topZ"]) == pytest.approx(side_box.max.Z, abs=1e-6)
        assert _value(item["boardUndersideZ"]) == pytest.approx(board_box["min"][2], abs=1e-6)

    assert len(result["lids"]) == 1
    reported_lid = result["lids"][0]
    assert reported_lid["featureId"] == "lid"
    _assert_bbox(reported_lid["outerBBox"], lid_box)
    seating_z = min(face.bounding_box().min.Z for face in _faces(service, "lid", "underside"))
    assert _value(reported_lid["seatingZ"]) == pytest.approx(seating_z, abs=1e-6)
    gaps = [
        min(
            lip.distance_to(inner_wall)
            for inner_wall in _faces(service, "base", "inner_wall")
            if lip.distance_to(inner_wall) > 1e-7
        )
        for lip in _faces(service, "lid", "lip_outer")
    ]
    assert _value(reported_lid["fitGap"]["min"]) == pytest.approx(min(gaps), abs=1e-6)
    assert _value(reported_lid["fitGap"]["max"]) == pytest.approx(max(gaps), abs=1e-6)
    lip_box = Compound(children=_faces(service, "lid", "lip_outer")).bounding_box()
    assert _value(reported_lid["lipDepth"]) == pytest.approx(
        lip_box.max.Z - lip_box.min.Z, abs=1e-6
    )

    assert len(result["ports"]) == 1
    port = result["ports"][0]
    assert (port["featureId"], port["footprintId"], port["reference"], port["wall"]) == (
        "port",
        "front-id",
        "J1",
        "left",
    )
    opening_box = _bbox(service._regen.opening_tools["port"])
    assert port["opening"]["axes"] == ["y", "z"]
    assert tuple(
        _value(port["opening"][key]) for key in ("uMin", "uMax", "vMin", "vMax")
    ) == pytest.approx(
        (
            opening_box["min"][1],
            opening_box["max"][1],
            opening_box["min"][2],
            opening_box["max"][2],
        ),
        abs=1e-6,
    )
    footprint = next(
        item
        for item in service._document.graph.nodes["controller_board"].mechanics["footprints"]
        if item["id"] == "front-id"
    )
    courtyard, _ = board_loop_wire(
        {"id": "front-id", "closed": True, "segments": footprint["courtyard"]}
    )
    courtyard_box = courtyard.bounding_box()
    connector_envelope = Box(
        opening_box["max"][0] - opening_box["min"][0],
        courtyard_box.max.Y - courtyard_box.min.Y,
        2,
        align=(Align.MIN, Align.MIN, Align.MIN),
    ).moved(
        Location(
            (
                opening_box["min"][0],
                courtyard_box.min.Y,
                board_box["max"][2] + 1,
            )
        )
    )
    envelope_box = _bbox(connector_envelope)
    measured_clearances = (
        envelope_box["min"][1] - opening_box["min"][1],
        opening_box["max"][1] - envelope_box["max"][1],
        envelope_box["min"][2] - opening_box["min"][2],
        opening_box["max"][2] - envelope_box["max"][2],
    )
    assert _value(port["clearance"]["min"]) == pytest.approx(min(measured_clearances), abs=1e-6)
    assert _value(port["clearance"]["max"]) == pytest.approx(max(measured_clearances), abs=1e-6)
    assert port["blocked"] is (
        _common_volume(service._regen.opening_tools["port"], Compound(children=[base, lid])) > 1e-7
    )

    assembled = Compound(children=[base, lid])
    assembled_box = _bbox(assembled)
    _assert_bbox(result["assembled"]["bbox"], assembled_box)
    assert tuple(_value(result["assembled"]["size"][axis]) for axis in "xyz") == pytest.approx(
        tuple(assembled_box["max"][i] - assembled_box["min"][i] for i in range(3)), abs=1e-6
    )
    assert _value(result["assembled"]["size"]["z"]) == pytest.approx(
        lid_box["max"][2] - base_box["min"][2], abs=1e-6
    )
    assert {part["featureId"] for part in result["terminalParts"]} == {"port", "lid"}
    assert any(warning["code"] == "CONNECTOR_HEIGHT_UNRESOLVED" for warning in result["warnings"])
    assert len(json.dumps(result, separators=(",", ":")).encode()) < 4096


@pytest.mark.anyio
async def test_enclosure_query_flags_duplicate_base_as_orphan(tmp_path):
    document = tmp_path / "duplicate.coalesce.json"
    duplicate = base_request(revision=2, request_id="duplicate")
    duplicate["featureId"] = "duplicate_base"
    *_, result = await run_server(
        document,
        [
            ("import", import_request(FIXTURES / "features.kicad_pcb")),
            ("feature", base_request()),
            ("feature", duplicate),
            ("query", {"select": {"kind": "enclosure", "base": "base"}}),
        ],
    )

    assert any(
        warning["code"] == "OTHER_MANUFACTURING_BODY" and warning["featureId"] == "duplicate_base"
        for warning in result["warnings"]
    )


@pytest.mark.anyio
async def test_enclosure_query_without_lid_or_ports_returns_empty_lists(tmp_path):
    document = tmp_path / "base-only.coalesce.json"
    *_, result = await run_server(
        document,
        [
            ("import", import_request(FIXTURES / "features.kicad_pcb")),
            ("feature", base_request()),
            ("query", {"select": {"kind": "enclosure", "base": "base"}}),
        ],
    )

    assert result["ok"] is True
    assert result["lids"] == []
    assert result["ports"] == []
    assert result["standoffs"]["count"] == 0
    assert result["standoffs"]["items"] == []


def test_enclosure_query_never_misreports_outer_edge_chamfer(tmp_path):
    service, _, _ = _base_service(tmp_path)
    graph = loads(dumps(service._document.graph))
    reference = Reference(
        "base", "between(outer_wall,rim)", Cardinality.ONE_OR_MORE
    )
    graph.add(Chamfer("outer_bevel", target="base", edges=[reference], distance=0.25))

    regenerated = regenerate(graph)
    result = inspect_enclosure(graph, regenerated, "base")
    truth = _independent_thicknesses(regenerated, "outer_bevel", reference.role)

    assert _thickness_outcome(result, truth) in {"exact", "unknown"}


def test_enclosure_query_never_misreports_aperture_edge_fillets(tmp_path):
    service, _, _ = _base_service(tmp_path, port=True)
    roles = [
        "between(outer_wall,port_top)",
        "between(inner_wall,port_top)",
        "between(outer_wall,port_side)",
        "between(inner_wall,port_side)",
        "between(outer_wall,port_bottom)",
        "between(inner_wall,port_bottom)",
    ]
    outcomes = {}
    for index, role in enumerate(roles):
        reference = Reference("port", role, Cardinality.ONE_OR_MORE)
        outcomes[role] = _fillet_case(service._document.graph, reference, f"aperture_{index}")

    assert set(outcomes.values()) == {"exact"}, outcomes


@pytest.mark.slow
def test_enclosure_query_thickness_sweep_never_returns_a_wrong_value(tmp_path):
    service, _, terminal = _base_service(tmp_path, standoffs=True, port=True)
    candidates = _terminal_edge_candidates(service._regen, terminal)
    counts = {
        "tried": len(candidates),
        "filleted": 0,
        "exact": 0,
        "unknown": 0,
        "wrong": 0,
    }
    edges_by_outcome = {"exact": [], "unknown": [], "wrong": []}
    for index, candidate in enumerate(candidates):
        try:
            outcome = _fillet_case(
                service._document.graph,
                _edge_reference(candidate),
                f"sweep_{index}",
            )
        except GraphError as error:
            assert error.error["code"] == "fillet_failed"
            continue
        counts["filleted"] += 1
        counts[outcome] += 1
        edges_by_outcome[outcome].append(candidate.key.role)

    print(
        "thickness sweep: "
        + ", ".join(f"{key}={value}" for key, value in counts.items())
    )
    assert counts["tried"] > 0
    assert counts["filleted"] == counts["exact"] + counts["unknown"] + counts["wrong"]
    print(f"thickness sweep outcomes: {edges_by_outcome}")
    assert counts["wrong"] == 0, {**counts, "edges": edges_by_outcome}
    # usefulness: only the four port-corner blends may be unknown.
    assert sorted(edges_by_outcome["unknown"]) == sorted(
        ["between(port_bottom,port_side)"] * 2 + ["between(port_side,port_top)"] * 2
    ), edges_by_outcome["unknown"]
    assert counts["exact"] == counts["filleted"] - 4, counts


@pytest.mark.parametrize("filleted_role", ["outer_wall", "inner_wall"])
def test_enclosure_query_measures_wall_fillet_from_final_faces(tmp_path, filleted_role):
    service, _, _ = _base_service(tmp_path)

    result, exact = _filleted_inspection(service, filleted_role)

    assert exact == pytest.approx(1.75, abs=1e-6)
    assert _value(result["base"]["minimumWallThickness"]) == pytest.approx(exact, abs=1e-6)


def test_enclosure_query_returns_summary_for_circular_outline(tmp_path):
    service = DocumentService(tmp_path / "circle.coalesce.json")
    assert service.import_board(import_request(FIXTURES / "circle.kicad_pcb"))["ok"]
    assert service.feature(base_request())["ok"]

    result = service.query({"select": {"kind": "enclosure", "base": "base"}})

    assert result["ok"] is True
    assert _value(result["base"]["minimumWallThickness"]) == pytest.approx(2)
    assert _value(result["base"]["floorThickness"]) == pytest.approx(2)
    assert result["cavity"]["boardClearance"]["left"]["status"] == "unknown"
    assert result["cavity"]["innerSize"]["x"]["status"] == "unknown"


def test_enclosure_query_returns_unknown_when_thickness_cannot_be_measured(
    tmp_path, monkeypatch
):
    service, _, _ = _base_service(tmp_path)

    def fail_distance(*_args):
        raise RuntimeError("distance unavailable")

    monkeypatch.setattr(
        "coalesce.enclosure_inspection.BRepExtrema_DistShapeShape", fail_distance
    )
    result = service.query({"select": {"kind": "enclosure", "base": "base"}})

    assert result["ok"] is True
    assert result["base"]["minimumWallThickness"] == {
        "status": "unknown",
        "reason": "distance unavailable",
    }
    assert result["base"]["floorThickness"] == {
        "status": "unknown",
        "reason": "distance unavailable",
    }


def test_enclosure_query_measures_wall_pocket_from_terminal_solid(tmp_path):
    service, revision, terminal = _base_service(tmp_path)
    assert _create_rectangle(
        service,
        revision=revision,
        sketch_id="wall_pocket",
        width=4,
        height=2,
        plane="YZ",
        offset=-3,
        center=(5, 2),
    )["ok"]
    assert service.feature(
        cut(
            request_id="wall-pocket-cut",
            revision=revision + 1,
            feature_id="wall_pocket_cut",
            target=terminal,
            tool="wall_pocket",
            depth=1.5,
            flip=True,
        )
    )["ok"]

    result = service.query({"select": {"kind": "enclosure", "base": "base"}})

    assert _value(result["base"]["minimumWallThickness"]) == pytest.approx(0.5, abs=1e-6)


def test_enclosure_query_reports_board_and_lid_interference_from_joined_rib(tmp_path):
    service, revision, terminal = _base_service(tmp_path, lid=True)
    assert _create_rectangle(
        service,
        revision=revision,
        sketch_id="rib",
        width=2,
        height=2,
        offset=-1,
        center=(10, 5),
    )["ok"]
    assert service.feature(
        _join("rib-join", revision + 1, "rib_join", "rib", terminal, 10)
    )["ok"]

    result = service.query({"select": {"kind": "enclosure", "base": "base"}})

    board_interference = result["cavity"]["boardInterference"]
    assert board_interference["volume"]["unit"] == "mm^3"
    assert board_interference["volume"]["value"] == pytest.approx(4.8)
    assert board_interference["parts"][0] == "controller_board"
    assert all(
        _value(clearance) <= 0 for clearance in result["cavity"]["boardClearance"].values()
    )
    lid_interference = result["lids"][0]["interference"]
    assert lid_interference["volume"]["unit"] == "mm^3"
    assert lid_interference["volume"]["value"] == pytest.approx(8)
    assert len(lid_interference["parts"]) == 2
    assert _value(result["lids"][0]["fitGap"]["min"]) <= 0


def test_enclosure_query_measures_port_aperture_after_later_cut(tmp_path):
    service, revision, terminal = _base_service(tmp_path, port=True)
    assert _create_rectangle(
        service,
        revision=revision,
        sketch_id="widen_port",
        width=3,
        height=3,
        plane="YZ",
        offset=-3,
        center=(8.5, 3.2),
    )["ok"]
    assert service.feature(
        cut(
            request_id="widen-port",
            revision=revision + 1,
            feature_id="widen_port_cut",
            target=terminal,
            tool="widen_port",
        )
    )["ok"]

    result = service.query({"select": {"kind": "enclosure", "base": "base"}})

    assert _value(result["ports"][0]["opening"]["uMax"]) == pytest.approx(10, abs=1e-6)


@pytest.mark.parametrize(
    ("sketch_id", "width", "center", "expected"),
    [
        ("half_boss", 3, (0.5, 3), "partial"),
        ("delete_boss", 6, (2, 3), "missing"),
        ("split_side", 0.2, (2, 3), "partial"),
    ],
)
def test_enclosure_query_classifies_cut_standoff_without_provenance_crash(
    tmp_path, sketch_id, width, center, expected
):
    service, revision, terminal = _base_service(tmp_path, standoffs=True)
    assert _create_rectangle(
        service,
        revision=revision,
        sketch_id=sketch_id,
        width=width,
        height=6,
        center=center,
    )["ok"]
    assert service.feature(
        cut(
            request_id=f"{sketch_id}-cut",
            revision=revision + 1,
            feature_id=f"{sketch_id}_cut",
            target=terminal,
            tool=sketch_id,
        )
    )["ok"]

    result = service.query({"select": {"kind": "enclosure", "base": "base"}})
    item = next(item for item in result["standoffs"]["items"] if item["holeId"] == FEATURE_HOLES[0])

    assert result["ok"] is True
    assert item["classification"] == expected
    assert item["axis"]["x"] == pytest.approx(2)
    assert FEATURE_HOLES[0] not in result["standoffs"]["coveredHoleIds"]


def test_enclosure_query_warns_about_unclassified_board_holes(tmp_path):
    board = tmp_path / "custom-library.kicad_pcb"
    shutil.copyfile(FIXTURES / "features.kicad_pcb", board)
    board.write_text(board.read_text().replace("MountingHole:M2", "CustomLibrary:M2"))
    service = DocumentService(tmp_path / "custom-library.coalesce.json")
    assert service.import_board(import_request(board))["ok"]
    assert service.feature(base_request())["ok"]

    result = service.query({"select": {"kind": "enclosure", "base": "base"}})

    warning = next(item for item in result["warnings"] if item["code"] == "UNCLASSIFIED_BOARD_HOLES")
    custom_hole = next(
        hole
        for hole in service._document.graph.nodes["controller_board"].mechanics["holes"]
        if hole["position"] == [2.0, 3.0]
    )
    assert warning["holeIds"] == [custom_hole["id"]]


@pytest.mark.anyio
async def test_published_query_schema_is_plain_and_accepts_enclosure(tmp_path):
    tools = {tool.name: tool for tool in await list_server_tools(tmp_path / "schema.coalesce.json")}
    schema = tools["query"].inputSchema

    assert schema["type"] == "object"
    assert not ({"oneOf", "anyOf", "allOf"} & schema.keys())
    validate({"select": {"kind": "enclosure", "base": "base"}}, schema)
    assert "before reporting enclosure dimensions" in tools["query"].description.lower()
