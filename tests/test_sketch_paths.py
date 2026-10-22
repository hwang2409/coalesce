from math import isclose, pi

import pytest
from build123d import GeomType
from test_mcp_vertical import call_server_tool

from coalesce import (
    Circle,
    Cut,
    Extrude,
    Fillet,
    Graph,
    GraphError,
    PathSegment,
    Plane,
    Rectangle,
    Reference,
    Sketch,
    SketchPath,
    SketchSegmentSource,
    Status,
    regenerate,
)
from coalesce.serialization import dumps, loads
from coalesce.service import DocumentService


def line(segment_id: str, x: float, y: float) -> PathSegment:
    return PathSegment(segment_id, "line", (x, y))


def arc(segment_id: str, through: tuple[float, float], x: float, y: float) -> PathSegment:
    return PathSegment(segment_id, "arc", (x, y), through)


def l_bracket(path_id: str = "bracket") -> SketchPath:
    # 40 x 30 L with a 5 mm thick base and a 5 mm thick upright.
    return SketchPath(
        path_id,
        start=(0, 0),
        segments=[
            line("base", 40, 0),
            line("end", 40, 5),
            line("inner", 5, 5),
            line("upright", 5, 30),
            line("cap", 0, 30),
            line("back", 0, 0),
        ],
    )


def slot(path_id: str = "slot") -> SketchPath:
    # 20 mm between arc centers, 6 mm wide: a stadium.
    return SketchPath(
        path_id,
        start=(-10, -3),
        segments=[
            line("lower", 10, -3),
            arc("right_end", (13, 0), 10, 3),
            line("upper", -10, 3),
            arc("left_end", (-13, 0), -10, -3),
        ],
    )


L_AREA = 40 * 5 + 5 * 25
SLOT_AREA = 20 * 6 + pi * 3**2


def extruded(entities, height: float = 4, plane: str = "XY") -> Graph:
    graph = Graph()
    graph.add(Sketch("sketch", plane=Plane(plane), entities=entities))
    graph.add(Extrude("body", sketch="sketch", height=height))
    return graph


def test_l_bracket_path_extrudes_with_exact_volume_and_bounds() -> None:
    shape = regenerate(extruded([l_bracket()])).shapes["body"]
    bounds = shape.bounding_box()

    assert shape.is_valid
    assert isclose(shape.volume, L_AREA * 4, rel_tol=1e-9)
    assert tuple(bounds.min) == pytest.approx((0, 0, 0))
    assert tuple(bounds.max) == pytest.approx((40, 30, 4))


def test_slot_path_arcs_are_exact_cylinders() -> None:
    shape = regenerate(extruded([slot()])).shapes["body"]
    cylinders = [face for face in shape.faces() if face.geom_type is GeomType.CYLINDER]

    assert isclose(shape.volume, SLOT_AREA * 4, rel_tol=1e-9)
    assert sorted(round(face.radius, 9) for face in cylinders) == [3, 3]


def test_every_path_segment_names_one_side_face() -> None:
    result = regenerate(extruded([l_bracket()]))

    for segment in l_bracket().segments:
        reference = Reference(
            "body",
            f"side({segment.id})",
            source=SketchSegmentSource("sketch", "bracket", segment.id),
        )
        resolution = result.resolve(reference)
        assert resolution.status is Status.OK, segment.id
        assert len(resolution.candidates) == 1


def test_clockwise_path_matches_counter_clockwise_path() -> None:
    clockwise = SketchPath(
        "bracket",
        start=(0, 0),
        segments=[
            line("back", 0, 30),
            line("cap", 5, 30),
            line("upright", 5, 5),
            line("inner", 40, 5),
            line("end", 40, 0),
            line("base", 0, 0),
        ],
    )
    result = regenerate(extruded([clockwise]))
    shape = result.shapes["body"]

    assert shape.is_valid
    assert isclose(shape.volume, L_AREA * 4, rel_tol=1e-9)
    assert tuple(shape.bounding_box().max) == pytest.approx((40, 30, 4))
    top = result.resolve(Reference("body", "cap_end"))
    assert top.status is Status.OK
    assert top.candidates[0].entity.center().Z == pytest.approx(4)


def test_circle_inside_path_becomes_a_hole() -> None:
    shape = regenerate(
        extruded([l_bracket(), Circle("mount", radius=1.5, position=(20, 2.5))])
    ).shapes["body"]

    assert isclose(shape.volume, (L_AREA - pi * 1.5**2) * 4, rel_tol=1e-9)


def test_path_inside_rectangle_becomes_a_shaped_pocket() -> None:
    shape = regenerate(
        extruded([Rectangle("plate", 40, 20), slot()])
    ).shapes["body"]

    assert isclose(shape.volume, (40 * 20 - SLOT_AREA) * 4, rel_tol=1e-9)


def test_path_cuts_a_slot_through_a_plate() -> None:
    graph = Graph()
    graph.add(Sketch("plate_sketch", entities=[Rectangle("plate", 40, 20)]))
    graph.add(Extrude("plate", sketch="plate_sketch", height=5))
    graph.add(Sketch("slot_sketch", plane=Plane("XY", offset=5), entities=[slot()]))
    graph.add(Cut("slot_cut", target="plate", tool="slot_sketch"))

    shape = regenerate(graph).shapes["slot_cut"]

    assert isclose(shape.volume, (40 * 20 - SLOT_AREA) * 5, rel_tol=1e-9)


def test_fillet_selects_inner_corner_by_segment_names() -> None:
    graph = extruded([l_bracket()], height=10)
    corner = Reference("body", "between(side(inner),side(upright))")
    graph.add(Fillet("round", target="body", edges=[corner], radius=2))

    shape = regenerate(graph).shapes["round"]
    blends = [face for face in shape.faces() if face.geom_type is GeomType.CYLINDER]

    # An inside fillet adds the corner square minus the quarter disc.
    assert shape.is_valid
    assert isclose(shape.volume, (L_AREA + 2**2 - pi * 2**2 / 4) * 10, rel_tol=1e-9)
    assert [round(face.radius, 9) for face in blends] == [2]


def test_segment_reference_survives_a_path_edit() -> None:
    graph = extruded([l_bracket()])
    reference = Reference(
        "body",
        "side(inner)",
        source=SketchSegmentSource("sketch", "bracket", "inner"),
    )
    before = regenerate(graph).resolve(reference)

    thicker = l_bracket()
    thicker.segments[1] = line("end", 40, 8)
    thicker.segments[2] = line("inner", 5, 8)
    graph.nodes["sketch"].replace_entities([thicker])
    after = regenerate(graph).resolve(reference)

    assert before.status is Status.OK and after.status is Status.OK
    assert before.candidates[0].entity.center().Y == pytest.approx(5)
    assert after.candidates[0].entity.center().Y == pytest.approx(8)


def test_rectangle_to_rounded_path_keeps_side_references() -> None:
    # Topological-naming plan case 3: adding corner arcs keeps the old wall keys.
    graph = Graph()
    graph.add(Sketch("sk", entities=[Rectangle("plate", 20, 10)]))
    graph.add(Extrude("body", sketch="sk", height=5))
    sides = ("bottom", "right", "top", "left")

    def side(segment: str) -> Reference:
        return Reference(
            "body", f"side({segment})", source=SketchSegmentSource("sk", "plate", segment)
        )

    before = regenerate(graph)
    x, y, r = 10.0, 5.0, 2.0
    k = r * (1 - 2**-0.5)
    graph.nodes["sk"].replace_entities([
        SketchPath(
            "plate",
            (-x + r, -y),
            [
                line("bottom", x - r, -y),
                arc("corner_br", (x - k, -y + k), x, -y + r),
                line("right", x, y - r),
                arc("corner_tr", (x - k, y - k), x - r, y),
                line("top", -x + r, y),
                arc("corner_tl", (-x + k, y - k), -x, y - r),
                line("left", -x, -y + r),
                arc("corner_bl", (-x + k, -y + k), -x + r, -y),
            ],
        )
    ])
    after = regenerate(graph)

    for name in sides:
        old, new = before.resolve(side(name)), after.resolve(side(name))
        assert old.status is Status.OK and new.status is Status.OK, name
        assert tuple(new.candidates[0].entity.center()) == pytest.approx(
            tuple(old.candidates[0].entity.center()), abs=1e-9
        )
    assert after.resolve(side("corner_br")).status is Status.OK
    assert isclose(
        after.shapes["body"].volume, (20 * 10 - (4 - pi) * r**2) * 5, rel_tol=1e-9
    )


def test_path_on_xz_follows_the_plane_frame() -> None:
    shape = regenerate(extruded([l_bracket()], plane="XZ")).shapes["body"]
    bounds = shape.bounding_box()

    assert isclose(shape.volume, L_AREA * 4, rel_tol=1e-9)
    assert tuple(bounds.min) == pytest.approx((0, -4, 0))
    assert tuple(bounds.max) == pytest.approx((40, 0, 30))


def test_path_round_trips_through_json() -> None:
    graph = extruded([slot()])

    restored = loads(dumps(graph))

    assert restored.nodes["sketch"].entities == [slot()]
    assert isclose(
        regenerate(restored).shapes["body"].volume, SLOT_AREA * 4, rel_tol=1e-9
    )


@pytest.mark.parametrize(
    ("path", "code", "segment"),
    [
        (
            SketchPath("p", (0, 0), [line("a", 10, 0), line("b", 10, 10), line("c", 0, 9)]),
            "OPEN_PATH",
            "c",
        ),
        (
            SketchPath("p", (0, 0), [line("a", 10, 0), line("b", 10, 0), line("c", 0, 0)]),
            "ZERO_LENGTH_SEGMENT",
            "b",
        ),
        (
            SketchPath("p", (0, 0), [arc("a", (5, 0), 10, 0), line("b", 0, 0)]),
            "COLLINEAR_ARC",
            "a",
        ),
        (
            SketchPath("p", (0, 0), [line("a", 10, 0), line("a", 10, 10), line("c", 0, 0)]),
            "DUPLICATE_SEGMENT_ID",
            "a",
        ),
        (
            SketchPath("p", (0, 0), [line("a", 10, 0)]),
            "PATH_TOO_SHORT",
            None,
        ),
        (
            SketchPath("p", (0, 0), [line("a", 10, 0), PathSegment("b", "bezier", (0, 0))]),
            "UNKNOWN_SEGMENT_KIND",
            "b",
        ),
        (
            SketchPath(
                "p",
                (0, 0),
                [line("a", 10, 10), line("b", 10, 0), line("c", 0, 10), line("d", 0, 0)],
            ),
            "SELF_INTERSECTING_PATH",
            None,
        ),
    ],
)
def test_invalid_paths_are_structured_errors(
    path: SketchPath, code: str, segment: str | None
) -> None:
    with pytest.raises(GraphError) as caught:
        regenerate(extruded([path]))

    assert caught.value.error["code"] == code
    assert caught.value.error["sketch"] == "sketch"
    assert caught.value.error["entity"] == "p"
    if segment is not None:
        assert caught.value.error["segment"] == segment


def test_open_path_reports_the_gap() -> None:
    path = SketchPath("p", (0, 0), [line("a", 10, 0), line("b", 10, 10), line("c", 0, 9)])

    with pytest.raises(GraphError) as caught:
        regenerate(extruded([path]))

    assert caught.value.error["gap"] == {"value": pytest.approx(9), "unit": "mm"}
    assert caught.value.error["end"] == [0, 9]


def path_payload(revision: int, request_id: str = "path-sketch") -> dict:
    return {
        "action": "create",
        "expectedRevision": revision,
        "requestId": request_id,
        "sketchId": "bracket_sketch",
        "plane": {"kind": "origin", "name": "XY"},
        "entities": [
            {
                "id": "bracket",
                "kind": "path",
                "start": [0, 0],
                "segments": [
                    {"id": "base", "kind": "line", "to": [40, 0]},
                    {"id": "end", "kind": "line", "to": [40, 5]},
                    {"id": "inner", "kind": "line", "to": [5, 5]},
                    {"id": "upright", "kind": "line", "to": [5, 30]},
                    {"id": "cap", "kind": "arc", "through": [2.5, 32.5], "to": [0, 30]},
                    {"id": "back", "kind": "line", "to": [0, 0]},
                ],
            }
        ],
    }


@pytest.mark.anyio
async def test_mcp_path_sketch_reports_segments_and_extrudes(tmp_path):
    service = DocumentService(tmp_path / "bracket.coalesce.json")

    sketch = await call_server_tool(service, "sketch", path_payload(0))
    feature = await call_server_tool(
        service,
        "feature",
        {
            "action": "create",
            "expectedRevision": 1,
            "requestId": "bracket-extrude",
            "featureId": "bracket_body",
            "kind": "extrude",
            "parameters": {
                "profile": {"sketchId": "bracket_sketch", "profileIds": ["bracket"]},
                "distance": {"value": 4, "unit": "mm"},
                "operation": "new_body",
                "direction": "normal",
            },
        },
    )
    metrics = await call_server_tool(
        service, "query", {"select": {"kind": "measurements"}}
    )

    assert sketch["ok"] is True
    assert sketch["entitySegments"] == {
        "bracket": ["base", "end", "inner", "upright", "cap", "back"]
    }
    assert feature["ok"] is True
    cap_area = pi * 2.5**2 / 2
    assert metrics["volume"]["value"] == pytest.approx((L_AREA + cap_area) * 4, rel=1e-9)
    assert metrics["bbox"]["max"] == {"x": 40, "y": pytest.approx(32.5), "z": 4, "unit": "mm"}


@pytest.mark.anyio
async def test_mcp_open_path_fails_without_advancing_revision(tmp_path):
    path = tmp_path / "open.coalesce.json"
    service = DocumentService(path)
    payload = path_payload(0)
    payload["entities"][0]["segments"][-1]["to"] = [0, 1]

    result = await call_server_tool(service, "sketch", payload)
    revision = await call_server_tool(service, "query", {"select": {"kind": "revision"}})

    assert result["ok"] is False
    assert result["error"]["code"] == "OPEN_PATH"
    assert "ends 1 mm from its start" in result["error"]["message"]
    assert result["error"]["segment"] == "back"
    assert revision["revision"] == 0


@pytest.mark.anyio
async def test_mcp_rejects_line_with_through_point_by_schema(tmp_path):
    service = DocumentService(tmp_path / "schema.coalesce.json")
    payload = path_payload(0)
    payload["entities"][0]["segments"][0]["through"] = [20, 1]

    result = await call_server_tool(service, "sketch", payload)

    assert result["ok"] is False
