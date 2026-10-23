import time

from build123d import Align, Box

from coalesce.document_store import Document, DocumentStore
from coalesce.graph import Extrude, Fillet, Graph, Rectangle, Sketch
from coalesce.references import Cardinality, Reference
from coalesce.service import DocumentService
from coalesce.viewer import ScenePart, ViewerSceneStore
from coalesce.viewer.scene import MAX_REPAIR_FEATURES, MAX_REPAIR_FIELDS


def _plate_graph() -> Graph:
    graph = Graph()
    graph.add(Sketch("plate_sketch", entities=[Rectangle("plate", 20, 20)]))
    graph.add(Extrude("plate", sketch="plate_sketch", height=10))
    edge = Reference("plate", "between(side(bottom),cap_end)", Cardinality.ONE)
    graph.add(Fillet("round", target="plate", edges=[edge], radius=4))
    return graph


def _update(feature_id: str, revision: int, request_id: str, kind: str, field: str, value):
    return {
        "action": "update",
        "expectedRevision": revision,
        "requestId": request_id,
        "featureId": feature_id,
        "kind": kind,
        "set": {field: {"value": value, "unit": "mm"}},
    }


def _create_fillet(revision: int, request_id: str, radius: float) -> dict:
    return {
        "action": "create",
        "expectedRevision": revision,
        "requestId": request_id,
        "featureId": "round",
        "kind": "fillet",
        "parameters": {
            "target": {"featureId": "plate"},
            "edges": [
                {
                    "feature": "plate",
                    "role": "between(side(bottom),cap_end)",
                    "cardinality": "one",
                }
            ],
            "radius": {"value": radius, "unit": "mm"},
        },
    }


def _wait_for(store: ViewerSceneStore, revision: int | None = None, failure: bool = False):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if failure and store.snapshot()[2] is not None:
            return store.snapshot()[2]
        if revision is not None and store.scene(revision) is not None:
            return store.scene(revision)
        time.sleep(0.02)
    raise AssertionError("viewer publication timed out")


def _edit(feature_id: str, kind: str, **values: float) -> dict:
    return {
        "featureId": feature_id,
        "kind": kind,
        "fields": {name: {"value": value, "unit": "mm"} for name, value in values.items()},
    }


def _part() -> ScenePart:
    return ScenePart("plate", Box(10, 10, 2, align=(Align.CENTER, Align.CENTER, Align.MIN)))


def test_failed_fillet_then_accepted_fillet_publishes_repair_values(tmp_path):
    path = tmp_path / "fillet.coalesce.json"
    DocumentStore(path).save(Document(_plate_graph(), revision=3))
    store = ViewerSceneStore()
    service = DocumentService(path, scene_store=store)
    _wait_for(store, revision=3)

    failed = service.feature(_update("round", 3, "too-large", "fillet", "radius", 1000))
    failure = _wait_for(store, failure=True)
    assert failed["ok"] is False
    assert failure["attemptedEdit"] == _edit("round", "fillet", radius=1000)

    accepted = service.feature(_update("round", 3, "repaired", "fillet", "radius", 1))
    scene = _wait_for(store, revision=4)
    assert accepted["ok"] is True
    expected_history = [
        {
            "featureId": "round",
            "kind": "fillet",
            "fields": {
                "radius": {
                    "attempted": {"value": 1000, "unit": "mm"},
                    "accepted": {"value": 1, "unit": "mm"},
                }
            },
        }
    ]
    assert scene["repairHistory"] == expected_history

    accepted_again = service.feature(
        _update("round", 4, "adjusted-again", "fillet", "radius", 0.5)
    )
    assert accepted_again["ok"] is True
    assert _wait_for(store, revision=5)["repairHistory"] == expected_history


def test_failed_fillet_then_delete_and_recreate_does_not_claim_repair(tmp_path):
    path = tmp_path / "recreated-fillet.coalesce.json"
    DocumentStore(path).save(Document(_plate_graph(), revision=3))
    store = ViewerSceneStore()
    service = DocumentService(path, scene_store=store)
    _wait_for(store, revision=3)

    assert service.feature(
        _update("round", 3, "too-large", "fillet", "radius", 1000)
    )["ok"] is False
    _wait_for(store, failure=True)
    assert service.feature(
        {
            "action": "delete",
            "expectedRevision": 3,
            "requestId": "delete-round",
            "featureId": "round",
        }
    )["ok"] is True
    assert _wait_for(store, revision=4)["repairHistory"] == []

    assert service.feature(_create_fillet(4, "recreate-round", 1))["ok"] is True
    assert _wait_for(store, revision=5)["repairHistory"] == []


def test_new_store_does_not_invent_repair_history():
    store = ViewerSceneStore()
    store.publish(1, [_part()])
    assert store.scene(1)["repairHistory"] == []


def test_unrelated_accepted_edit_does_not_repair_pending_fillet():
    store = ViewerSceneStore()
    store.publish(1, [_part()])
    store.publish_failure(
        ["round"], {"code": "KERNEL_OPERATION_FAILED", "message": "failed"},
        _edit("round", "fillet", radius=1000),
    )
    store.publish(
        2,
        [_part()],
        {"featureId": "plate"},
        accepted_edit=_edit("plate", "extrude", distance=2),
    )
    assert store.scene(2)["repairHistory"] == []

    store.publish(
        3,
        [_part()],
        {"featureId": "round"},
        accepted_edit=_edit("round", "fillet", radius=1),
    )
    assert store.scene(3)["repairHistory"][0]["featureId"] == "round"


def test_different_field_on_same_feature_consumes_attempt_without_repair():
    store = ViewerSceneStore()
    store.publish(1, [_part()])
    store.publish_failure(
        ["round"], {"code": "KERNEL_OPERATION_FAILED", "message": "failed"},
        _edit("round", "fillet", radius=1000),
    )

    store.publish(2, [_part()], {"featureId": "round"}, accepted_edit=_edit("round", "fillet", distance=1))
    assert store.scene(2)["repairHistory"] == []

    store.publish(3, [_part()], {"featureId": "round"}, accepted_edit=_edit("round", "fillet", radius=1))
    assert store.scene(3)["repairHistory"] == []


def test_invalid_unvalidated_quantity_makes_no_attempt_record(tmp_path):
    path = tmp_path / "invalid.coalesce.json"
    DocumentStore(path).save(Document(_plate_graph(), revision=3))
    store = ViewerSceneStore()
    service = DocumentService(path, scene_store=store)
    _wait_for(store, revision=3)

    result = service.feature(_update("round", 3, "not-a-number", "fillet", "radius", "huge"))
    failure = _wait_for(store, failure=True)
    assert result["error"]["code"] == "INVALID_PARAMETER"
    assert "attemptedEdit" not in failure

    service.feature(_update("round", 3, "valid", "fillet", "radius", 1))
    assert _wait_for(store, revision=4)["repairHistory"] == []


def test_repair_tracking_bounds_features_and_fields():
    store = ViewerSceneStore()
    store.publish(1, [_part()])
    for index in range(MAX_REPAIR_FEATURES + 1):
        store.publish_failure(
            [f"feature-{index}"], {"code": "FAILED", "message": "failed"},
            _edit(
                f"feature-{index}",
                "fillet",
                **{f"field-{field}": field for field in range(MAX_REPAIR_FIELDS + 1)},
            ),
        )

    store.publish(
        2,
        [_part()],
        {"featureId": "feature-0"},
        accepted_edit=_edit("feature-0", "fillet", **{"field-0": 1}),
    )
    assert store.scene(2)["repairHistory"] == []

    last = f"feature-{MAX_REPAIR_FEATURES}"
    store.publish(
        3,
        [_part()],
        {"featureId": last},
        accepted_edit=_edit(
            last,
            "fillet",
            **{f"field-{field}": field / 2 for field in range(MAX_REPAIR_FIELDS + 1)},
        ),
    )
    repair = store.scene(3)["repairHistory"][0]
    assert repair["featureId"] == last
    assert len(repair["fields"]) == MAX_REPAIR_FIELDS
