from math import isclose

import pytest
from build123d import GeomType, Solid, Vertex
from build123d import Rectangle as BuildRectangle

from coalesce import (
    Cardinality,
    Chamfer,
    Extrude,
    Graph,
    GraphError,
    Rectangle,
    Reference,
    Sketch,
    Status,
    regenerate,
)
from coalesce.edge_treatment import chamfer_shape
from coalesce.provenance import descriptor
from coalesce.references import Candidate, ElementKey


def box_graph(width=20, depth=10, height=5):
    graph = Graph()
    graph.add(Sketch("box_sketch", entities=[Rectangle("outline", width, depth)]))
    graph.add(Extrude("box", sketch="box_sketch", height=height))
    return graph


def top_edges():
    return [
        Reference("box", f"between(side({side}),cap_end)")
        for side in ("bottom", "right", "top", "left")
    ]


def test_symmetric_chamfer_has_analytic_volume_and_faces_keyed_by_source_edge():
    graph = box_graph()
    graph.add(Chamfer("bevel", target="box", edges=top_edges(), distance=1))

    result = regenerate(graph)
    chamfers = [
        candidate
        for candidate in result.provenance["bevel"]
        if candidate.key.role.startswith("chamfer(")
    ]

    expected_removed = 30 - 4 / 3
    assert isclose(result.shapes["bevel"].volume, 1000 - expected_removed, abs_tol=1e-7)
    assert len(chamfers) == 4
    assert all(face.entity.geom_type is GeomType.PLANE for face in chamfers)
    assert {face.key.source.role for face in chamfers} == {edge.role for edge in top_edges()}
    for edge in top_edges():
        consumed = result.resolve(edge)
        assert consumed.status is Status.EMPTY
        assert consumed.error is not None
        assert "chamfer(" in consumed.error["suggestion"]


def test_chamfer_reference_survives_upstream_dimension_change():
    graph = box_graph()
    graph.add(Chamfer("bevel", target="box", edges=top_edges(), distance=1))

    first = regenerate(graph)
    graph.nodes["box_sketch"].entities[0].width = 30
    second = regenerate(graph)

    assert len(first.provenance["bevel"]) > 0
    assert second.shapes["bevel"].volume > first.shapes["bevel"].volume
    assert (
        len([item for item in second.provenance["bevel"] if item.key.role.startswith("chamfer(")])
        == 4
    )


def test_oversized_chamfer_is_structured_and_atomic():
    graph = box_graph()
    graph.add(Chamfer("bevel", target="box", edges=top_edges(), distance=100))

    with pytest.raises(GraphError) as caught:
        regenerate(graph)

    error = caught.value.error
    assert error["code"] == "chamfer_failed"
    assert error["distance"] == 100
    assert len(error["edges"]) == 4
    assert error["safeRange"] == "unknown"
    assert "reduce the chamfer distance" in error["hint"]


def test_chamfer_reports_real_occt_failure_at_four_edge_apex_atomically():
    pyramid = Solid.make_loft([BuildRectangle(10, 10).wire(), Vertex(0, 0, 10)], ruled=True)
    apex = next(vertex for vertex in pyramid.vertices() if vertex.center().Z > 9)
    apex_edges = [
        edge for edge in pyramid.edges() if any(apex.is_same(vertex) for vertex in edge.vertices())
    ]
    selected = [
        Candidate(ElementKey("pyramid", f"apex_{index}"), edge, descriptor(edge))
        for index, edge in enumerate(apex_edges)
    ]

    with pytest.raises(GraphError) as caught:
        chamfer_shape("bevel", pyramid, selected, selected, 6)

    error = caught.value.error
    assert len(apex_edges) == 4
    assert error["code"] == "chamfer_failed"
    assert error["safeRange"] == "unknown"
    assert [edge["role"] for edge in error["edges"]] == [f"apex_{index}" for index in range(4)]


def test_chamfer_rejects_ambiguous_reference_without_selecting_a_subset():
    graph = Graph()
    graph.add(
        Sketch(
            "parts_sketch",
            entities=[
                Rectangle("left", 2, 2, position=(-2, 0)),
                Rectangle("right", 2, 2, position=(2, 0)),
            ],
        )
    )
    graph.add(Extrude("parts", sketch="parts_sketch", height=2))
    reference = Reference("parts", "between(side(bottom),cap_end)", Cardinality.ONE)
    graph.add(Chamfer("bevel", target="parts", edges=[reference], distance=0.25))

    with pytest.raises(GraphError) as caught:
        regenerate(graph)

    assert caught.value.error["code"] == "reference_failed"
    assert caught.value.error["resolution"]["status"] == "AMBIGUOUS"
    assert len(caught.value.error["resolution"]["candidates"]) == 2
