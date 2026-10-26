import json
import math

import pytest

from coalesce.assembly import (
    Assembly,
    Occurrence,
    RigidTransform,
    assembly_from_dict,
    assembly_to_dict,
    normalize_transform,
)
from coalesce.document_store import DocumentStore
from coalesce.graph import GraphError


def test_transform_normalization_and_round_trip():
    transform = normalize_transform(
        {"translation": {"value": [1, 2, 3], "unit": "mm"}, "rotation": {"axis": [0, 0, 4], "angle": {"value": math.pi * 3, "unit": "rad"}}}
    )
    assert transform == RigidTransform((1.0, 2.0, 3.0), (0.0, 0.0, 1.0), -180.0)
    zero = normalize_transform(
        {"translation": {"value": [0, 0, 0], "unit": "mm"}, "rotation": {"axis": [0, 0, 0], "angle": {"value": 360, "unit": "deg"}}}
    )
    assert zero.axis == (0.0, 0.0, 1.0)
    assert zero.angle_degrees == 0.0

    with pytest.raises(ValueError, match="axis"):
        normalize_transform(
            {"translation": {"value": [0, 0, 0], "unit": "mm"}, "rotation": {"axis": [0, 0, 0], "angle": {"value": 1, "unit": "deg"}}}
        )
    with pytest.raises(ValueError, match="finite"):
        normalize_transform(
            {"translation": {"value": [math.inf, 0, 0], "unit": "mm"}, "rotation": {"axis": [0, 0, 1], "angle": {"value": 0, "unit": "deg"}}}
        )


def test_assembly_serialization_is_sorted_and_rejects_duplicate_ids():
    transform = RigidTransform((0, 0, 0), (0, 0, 1), 0)
    assembly = Assembly({
        "z": Occurrence("z", "part/z", transform),
        "a": Occurrence("a", "part/a", transform),
    })
    assert [item["id"] for item in assembly_to_dict(assembly)["occurrences"]] == ["a", "z"]
    assert assembly_from_dict(assembly_to_dict(assembly)) == assembly

    payload = assembly_to_dict(assembly)
    payload["occurrences"].append(payload["occurrences"][0])
    with pytest.raises(GraphError) as caught:
        assembly_from_dict(payload)
    assert caught.value.error["code"] == "INVALID_DOCUMENT"


def test_document_store_dispatches_strictly_by_format_version(tmp_path):
    path = tmp_path / "part.coalesce.json"
    persisted = assembly_to_dict(
        Assembly(
            {
                "ignored": Occurrence(
                    "ignored", "pin/body", RigidTransform((1, 2, 3), (0, 1, 0), 45)
                )
            }
        )
    )
    path.write_text(
        json.dumps(
            {
                "formatVersion": 1,
                "revision": 2,
                "graph": {"nodes": []},
                "assembly": persisted,
            }
        )
    )
    assert DocumentStore(path).load().assembly == Assembly()

    for payload in (
        {"formatVersion": 2, "graph": {"nodes": []}},
        {"formatVersion": 2, "graph": {"nodes": []}, "assembly": None},
    ):
        path.write_text(json.dumps(payload))
        with pytest.raises(GraphError, match="requires assembly") as missing:
            DocumentStore(path).load()
        assert missing.value.error["code"] == "INVALID_DOCUMENT"

    path.write_text(
        json.dumps({"formatVersion": 4, "graph": {"nodes": []}, "assembly": persisted})
    )
    before = path.read_bytes()
    with pytest.raises(GraphError, match="version 4.*newer coalesce") as future:
        DocumentStore(path).load()
    assert future.value.error["code"] == "INVALID_DOCUMENT"
    assert path.read_bytes() == before


def test_document_store_v3_round_trip_bytes_are_stable(tmp_path):
    path = tmp_path / "part.coalesce.json"
    store = DocumentStore(path)
    transform = normalize_transform(
        {
            "translation": {"value": [1, 2, 3], "unit": "mm"},
            "rotation": {"axis": [0, 1, 0], "angle": {"value": 45, "unit": "deg"}},
        }
    )
    document = store.load()
    document.assembly = Assembly({"pin-2": Occurrence("pin-2", "pin/body", transform)})

    store.save(document)
    before = path.read_bytes()
    store.save(store.load())

    assert path.read_bytes() == before


def test_ground_mates_round_trip_sorted_and_reject_invalid_documents():
    payload = {
        "occurrences": [
            {
                "id": "plate",
                "partId": "plate/body",
                "transform": {
                    "translation": [0, 0, 0],
                    "rotation": {"axis": [0, 0, 1], "angleDegrees": 0},
                },
            }
        ],
        "mates": [
            {"id": "z-ground", "kind": "ground", "occurrenceId": "plate"},
        ],
    }
    assembly = assembly_from_dict(payload)
    assert assembly_to_dict(assembly) == payload

    duplicate = {**payload, "mates": [payload["mates"][0], payload["mates"][0]]}
    with pytest.raises(GraphError, match="duplicate assembly mate id") as duplicate_error:
        assembly_from_dict(duplicate)
    assert duplicate_error.value.error["code"] == "INVALID_DOCUMENT"

    conflicting = {
        **payload,
        "mates": [
            payload["mates"][0],
            {"id": "second-ground", "kind": "ground", "occurrenceId": "plate"},
        ],
    }
    with pytest.raises(GraphError, match="more than one ground") as conflict_error:
        assembly_from_dict(conflicting)
    assert conflict_error.value.error["code"] == "INVALID_DOCUMENT"


def test_document_store_v3_mate_round_trip_is_byte_stable_and_v2_upgrades_on_save(tmp_path):
    path = tmp_path / "mates.coalesce.json"
    old_assembly = {
        "occurrences": [
            {
                "id": "old",
                "partId": "part/body",
                "transform": {
                    "translation": [0, 0, 0],
                    "rotation": {"axis": [0, 0, 1], "angleDegrees": 0},
                },
            }
        ]
    }
    path.write_text(json.dumps({"formatVersion": 2, "graph": {"nodes": []}, "assembly": old_assembly}))
    loaded_old = DocumentStore(path).load().assembly
    assert list(loaded_old.occurrences) == ["old"]
    assert loaded_old.mates == {}
    old_store = DocumentStore(path)
    old_store.save(old_store.load())
    upgraded = json.loads(path.read_text())
    assert upgraded["formatVersion"] == 3
    assert upgraded["assembly"] == {**old_assembly, "mates": []}
    first_upgrade = path.read_bytes()
    old_store.save(old_store.load())
    assert path.read_bytes() == first_upgrade

    path.write_text(json.dumps({"formatVersion": 3, "graph": {"nodes": []}, "assembly": old_assembly}))
    with pytest.raises(GraphError, match="occurrences and mates"):
        DocumentStore(path).load()

    path.write_text(
        json.dumps(
            {
                "formatVersion": 3,
                "graph": {"nodes": []},
                "assembly": {"occurrences": [], "mates": []},
            }
        )
    )
    store = DocumentStore(path)
    store.save(store.load())
    before = path.read_bytes()
    store.save(store.load())
    assert path.read_bytes() == before

    path.write_text(json.dumps({"formatVersion": 4, "graph": {"nodes": []}, "assembly": {"occurrences": [], "mates": []}}))
    before = path.read_bytes()
    with pytest.raises(GraphError, match="version 4.*newer coalesce"):
        store.load()
    assert path.read_bytes() == before
