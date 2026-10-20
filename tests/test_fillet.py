from dataclasses import replace
from math import isclose, pi

import pytest
from build123d import GeomType

from coalesce import (
    Cardinality,
    Cut,
    Extrude,
    Fillet,
    Graph,
    GraphError,
    Rectangle,
    Reference,
    Sketch,
    Status,
    regenerate,
)


def base_graph() -> Graph:
    graph = Graph()
    graph.add(Sketch("base_sketch", entities=[Rectangle("plate", 10, 10)]))
    graph.add(Extrude("base", sketch="base_sketch", height=10))
    return graph


def top_bottom_edge(cardinality: Cardinality = Cardinality.ONE) -> Reference:
    return Reference(
        "base",
        "between(side(bottom),cap_end)",
        cardinality,
    )


def add_slot(graph: Graph) -> None:
    graph.add(
        Sketch(
            "slot_sketch",
            entities=[Rectangle("slot", 2, 20)],
            position=(0, 0, 10),
        )
    )
    graph.add(Cut("slot", target="base", tool="slot_sketch"))


def test_case_13_fillet_consumes_edge_and_names_blend_replacement() -> None:
    graph = base_graph()
    graph.add(Fillet("round", target="base", edges=[top_bottom_edge()], radius=1))

    result = regenerate(graph)
    consumed = result.resolve(top_bottom_edge())
    blends = [
        candidate
        for candidate in result.provenance["round"]
        if candidate.key.role.startswith("blend(")
    ]

    assert consumed.status is Status.EMPTY
    assert consumed.error is not None
    assert "blend(" in consumed.error["suggestion"]
    assert "round" in consumed.error["suggestion"]
    assert len(blends) == 1
    assert blends[0].entity.geom_type is not GeomType.PLANE
    expected_removed = (1 - pi / 4) * 10
    assert isclose(result.shapes["round"].volume, 1000 - expected_removed, abs_tol=1e-7)


def test_unresolved_exact_blend_source_returns_structured_empty() -> None:
    graph = base_graph()
    graph.add(Fillet("round", target="base", edges=[top_bottom_edge()], radius=1))
    result = regenerate(graph)
    blend = next(
        candidate
        for candidate in result.provenance["round"]
        if candidate.key.role.startswith("blend(")
    )
    missing_source = replace(blend.key.source, role="missing")

    resolution = result.resolve(
        Reference("round", blend.key.role, source=missing_source, body=blend.key.body)
    )

    assert resolution.status is Status.EMPTY
    assert resolution.candidates == []
    assert resolution.error is not None
    assert resolution.error["status"] == Status.EMPTY.value
    assert resolution.error["reference"]["source"]["kind"] == "element_key"


def test_case_14_fillet_keeps_adjacent_cap_reference_ok() -> None:
    graph = base_graph()
    graph.add(Fillet("round", target="base", edges=[top_bottom_edge()], radius=1))

    cap = regenerate(graph).resolve(Reference("base", "cap_end"))

    assert cap.status is Status.OK
    assert cap.candidates[0].entity.area < 100


def test_case_15_split_edge_obeys_fillet_reference_cardinality() -> None:
    graph = base_graph()
    add_slot(graph)
    graph.add(
        Fillet(
            "round",
            target="slot",
            edges=[top_bottom_edge(Cardinality.ONE_OR_MORE)],
            radius=0.5,
        )
    )

    result = regenerate(graph)
    blends = [
        candidate
        for candidate in result.provenance["round"]
        if candidate.key.role.startswith("blend(")
    ]
    assert len(blends) == 2
    assert len(result.shapes["round"].solids()) == 2

    graph.nodes["round"].edges = [top_bottom_edge()]
    with pytest.raises(GraphError) as caught:
        regenerate(graph)

    assert caught.value.error["code"] == "reference_failed"
    assert caught.value.error["resolution"]["status"] == Status.SPLIT.value
    assert len(caught.value.error["resolution"]["candidates"]) == 2


def test_case_16_second_fillet_uses_edge_involving_generated_blend_face() -> None:
    graph = base_graph()
    vertical_edge = Reference("base", "between(side(bottom),side(left))")
    graph.add(Fillet("round", target="base", edges=[vertical_edge], radius=1))
    first = regenerate(graph)
    generated_edge = next(
        candidate
        for candidate in first.provenance["round"]
        if candidate.key.role.startswith("between(blend(")
        and candidate.key.role.endswith(",cap_end)")
    )
    graph.add(
        Fillet(
            "round_again",
            target="round",
            edges=[Reference("round", generated_edge.key.role)],
            radius=0.25,
        )
    )

    result = regenerate(graph)

    assert result.shapes["round_again"].volume < result.shapes["round"].volume
    assert any(
        candidate.key.role.startswith("blend(")
        and candidate.key.source == generated_edge.key
        for candidate in result.provenance["round_again"]
    )


def test_fillet_accepts_edge_adjacent_to_cut_generated_wall_and_floor() -> None:
    graph = base_graph()
    graph.add(
        Sketch(
            "pocket_sketch",
            entities=[Rectangle("pocket", 4, 4)],
            position=(0, 0, 10),
        )
    )
    graph.add(Cut("pocket", target="base", tool="pocket_sketch", depth=2))
    edge_ref = Reference("pocket", "between(pocket_floor,side(bottom))")
    graph.add(Fillet("pocket_round", target="pocket", edges=[edge_ref], radius=0.5))

    result = regenerate(graph)
    blends = [
        candidate
        for candidate in result.provenance["pocket_round"]
        if candidate.key.role.startswith("blend(")
    ]

    assert len(blends) == 1
    assert blends[0].key.source.feature == "pocket"
    assert blends[0].key.source.role == edge_ref.role
    assert result.shapes["pocket_round"].volume > result.shapes["pocket"].volume


@pytest.mark.parametrize(
    ("graph", "reference", "status"),
    [
        (base_graph(), Reference("base", "missing"), Status.EMPTY),
        (
            Graph(),
            Reference("parts", "between(side(bottom),cap_end)"),
            Status.AMBIGUOUS,
        ),
    ],
)
def test_fillet_rejects_unresolved_or_ambiguous_edge_reference(
    graph: Graph, reference: Reference, status: Status
) -> None:
    if status is Status.AMBIGUOUS:
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
    graph.add(Fillet("round", target=reference.feature, edges=[reference], radius=0.25))

    with pytest.raises(GraphError) as caught:
        regenerate(graph)

    assert caught.value.error["code"] == "reference_failed"
    assert caught.value.error["resolution"]["status"] == status.value
    assert caught.value.error["resolution"]["reference"] == reference.to_dict()


def test_fillet_radius_too_large_is_structured_without_kernel_wording() -> None:
    graph = base_graph()
    graph.add(Fillet("round", target="base", edges=[top_bottom_edge()], radius=100))

    with pytest.raises(GraphError) as caught:
        regenerate(graph)

    error = caught.value.error
    assert error["code"] == "fillet_failed"
    assert error["radius"] == 100
    assert len(error["edges"]) == 1
    assert "max_fillet" not in error["message"]
    assert "build123d" not in error["message"].lower()
    assert "occt" not in error["message"].lower()
