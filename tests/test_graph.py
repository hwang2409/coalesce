from coalesce import *
from coalesce.regen import regenerate


def base(height=10):
    g = Graph()
    g.add(Sketch("sk", entities=[Rectangle("bottom", 10, 10)]))
    g.add(Extrude("base", sketch="sk", height=height))
    return g


def test_case_1_cap_end_height_geometry():
    g = base(); regenerate(g)
    ref = Reference("base", "cap_end")
    first = g.resolve(ref); assert first.status is Status.OK
    assert round(first.candidates[0].entity.center().Z) == 10
    g.nodes["base"].height = 20; regenerate(g)
    second = g.resolve(ref); assert second.status is Status.OK
    assert round(second.candidates[0].entity.center().Z) == 20


def test_case_2_rectangle_wall_follows_width():
    g = base(); regenerate(g); ref = Reference("base", "side(bottom)")
    assert g.resolve(ref).status is Status.OK
    g.nodes["sk"].entities[0].width = 20; regenerate(g)
    wall = g.resolve(ref); assert wall.status is Status.OK
    assert wall.candidates[0].entity.normal_at().Y < 0
    assert round(wall.candidates[0].entity.center().Y) == -5


def test_case_4_hole_edge_and_radius_change():
    g = base(); g.add(Hole("hole", target="base", diameter=4)); regenerate(g)
    wall = g.resolve(Reference("hole", "hole_wall")); assert wall.status is Status.OK
    assert round(wall.candidates[0].entity.radius, 5) == 2
    g.nodes["hole"].diameter = 6; regenerate(g)
    assert round(g.resolve(Reference("hole", "hole_wall")).candidates[0].entity.radius, 5) == 3


def test_case_8_delete_through_graph_api():
    g = base(); regenerate(g); g.remove("base")
    result = g.resolve(Reference("base", "cap_end"))
    assert result.status is Status.EMPTY and result.error["feature"] == "base"


def test_case_9_slot_splits_cap_and_cardinality_collects_both():
    g = base(); g.add(Sketch("slot", entities=[Rectangle("slot", 2, 20)])); g.add(Cut("cut", target="base", tool="slot")); regenerate(g)
    one = g.resolve(Reference("cut", "cap_end")); assert one.status is Status.SPLIT and len(one.candidates) == 2
    many = g.resolve(Reference("cut", "cap_end", Cardinality.ONE_OR_MORE)); assert many.status is Status.OK
    assert len(many.candidates) == 2
    assert round(sum(c.entity.area for c in many.candidates)) == 80


def test_case_13_fillet_changes_volume_and_creates_blend():
    g = base(); g.add(Fillet("round", target="base", edges=[Reference("base", "edge(side(bottom),cap_end)")], radius=1)); regenerate(g)
    assert g.shapes["round"].volume < g.shapes["base"].volume
    blend = g.resolve(Reference("round", "blend")); assert blend.status is Status.OK
    assert any(f.geom_type.name != "PLANE" for f in [c.entity for c in blend.candidates])


def test_case_17_equal_max_faces_are_not_silent():
    g = base(); g.add(Sketch("boss_sk", entities=[Circle("boss", 2)])); g.add(Extrude("boss", sketch="boss_sk", height=10)); regenerate(g)
    tops = [f for f in g.shapes["base"].faces() if round(f.normal_at().Z) == 1]
    assert len(tops) == 1 and round(tops[0].center().Z) == 10
    assert round(g.shapes["boss"].faces()[-1].center().Z) == 10


def test_case_23_json_round_trip_same_volume():
    from coalesce.serialization import loads
    g = base(); g.add(Hole("hole", target="base", diameter=2)); regenerate(g)
    clone = loads(g.to_json()); regenerate(clone)
    assert round(clone.shapes["hole"].volume, 6) == round(g.shapes["hole"].volume, 6)
    assert [c.key for c in clone.provenance["base"]] == [c.key for c in g.provenance["base"]]
