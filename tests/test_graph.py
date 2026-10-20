import pytest

from coalesce import *
from coalesce.regen import regenerate


def base(height=10):
    g = Graph()
    g.add(Sketch("sk", entities=[Rectangle("bottom", 10, 10), Rectangle("left", 10, 10)]))
    g.add(Extrude("base", sketch="sk", height=height))
    return g


def test_case_1_cap_end_survives_height_change():
    g = base()
    regenerate(g)
    ref = Reference("base", "cap_end")
    assert g.resolve(ref).status is Status.OK
    g.nodes["base"].height = 20
    regenerate(g)
    assert g.resolve(ref).status is Status.OK


def test_case_2_segment_wall_is_named():
    g = base()
    regenerate(g)
    assert g.resolve(Reference("base", "side(bottom)", source="bottom")).status is Status.OK


def test_case_4_hole_wall_is_resolved():
    g = base()
    g.add(Hole("hole", target="base", diameter=2))
    regenerate(g)
    assert g.resolve(Reference("hole", "hole_wall")).status is Status.OK


def test_case_8_deleted_feature_is_empty():
    g = base()
    regenerate(g)
    del g.nodes["base"]
    assert g.resolve(Reference("base", "cap_end")).status is Status.EMPTY


def test_case_9_one_cardinality_reports_split():
    g = Graph()
    g.provenance["cut"] = [
        __import__("coalesce.references", fromlist=["Candidate"]).Candidate(
            __import__("coalesce.references", fromlist=["ElementKey"]).ElementKey("cut", "cap_end")
        ),
        __import__("coalesce.references", fromlist=["Candidate"]).Candidate(
            __import__("coalesce.references", fromlist=["ElementKey"]).ElementKey("cut", "cap_end")
        ),
    ]
    assert g.resolve(Reference("cut", "cap_end")).status is Status.SPLIT


def test_case_13_fillet_has_blend_provenance():
    g = base()
    g.add(Fillet("round", target="base", edges=[Reference("base", "cap_end")], radius=1))
    regenerate(g)
    assert g.resolve(Reference("round", "blend")).status is Status.OK


def test_case_17_no_index_selector():
    g = base()
    regenerate(g)
    assert g.resolve(Reference("base", "cap_end")).status is Status.OK


def test_case_23_unknown_id_is_structured():
    g = Graph()
    g.add(Extrude("bad", sketch="missing"))
    with pytest.raises(Exception) as error:
        g.validate()
    assert error.value.error["code"] == "unknown_id"


def test_json_round_trip_preserves_ids():
    from coalesce.serialization import loads

    g = base()
    assert list(loads(g.to_json()).nodes) == ["sk", "base"]
