import json

import pytest
from build123d import Compound, Face, Shape
from jsonschema import validate
from OCP.BRepAlgoAPI import BRepAlgoAPI_Common
from test_mcp_truthful_contract import list_server_tools
from test_mcp_vertical import run_server
from test_pcb_enclosure import FEATURE_HOLES, FIXTURES, base_request, import_request
from test_pcb_lid import lid_request
from test_port_cutout import port_request

from coalesce.pcb import board_loop_wire
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


def _assert_bbox(actual, expected):
    assert actual["min"]["unit"] == actual["max"]["unit"] == "mm"
    assert tuple(actual["min"][axis] for axis in "xyz") == pytest.approx(expected["min"], abs=1e-6)
    assert tuple(actual["max"][axis] for axis in "xyz") == pytest.approx(expected["max"], abs=1e-6)


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
        [pilot] = [
            candidate.entity
            for candidate in service._regen.provenance["mounts"]
            if candidate.key.role == "pilot_wall" and candidate.key.source == item["holeId"]
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
        assert _value(item["bossDiameter"]) == pytest.approx(side.radius * 2, abs=1e-6)
        assert _value(item["pilotDiameter"]) == pytest.approx(pilot.radius * 2, abs=1e-6)
        assert _value(item["topZ"]) == pytest.approx(side_box.max.Z, abs=1e-6)
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
    measured_clearances = (
        courtyard_box.min.Y - opening_box["min"][1],
        opening_box["max"][1] - courtyard_box.max.Y,
        board_box["max"][2] + 1 - opening_box["min"][2],
        opening_box["max"][2] - (board_box["max"][2] + 3),
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


@pytest.mark.anyio
async def test_published_query_schema_is_plain_and_accepts_enclosure(tmp_path):
    tools = {tool.name: tool for tool in await list_server_tools(tmp_path / "schema.coalesce.json")}
    schema = tools["query"].inputSchema

    assert schema["type"] == "object"
    assert not ({"oneOf", "anyOf", "allOf"} & schema.keys())
    validate({"select": {"kind": "enclosure", "base": "base"}}, schema)
    assert "before reporting enclosure dimensions" in tools["query"].description.lower()
