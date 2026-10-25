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


def test_document_store_reads_v1_empty_and_persists_v2_exactly(tmp_path):
    path = tmp_path / "part.coalesce.json"
    path.write_text(json.dumps({"formatVersion": 1, "revision": 2, "graph": {"nodes": []}}))
    store = DocumentStore(path)
    loaded = store.load()
    assert loaded.assembly == Assembly()

    transform = RigidTransform((1, 2, 3), (0, 1, 0), 45)
    loaded.assembly = Assembly({"pin-2": Occurrence("pin-2", "pin/body", transform)})
    store.save(loaded)
    payload = json.loads(path.read_text())
    assert payload["formatVersion"] == 2
    assert DocumentStore(path).load().assembly == loaded.assembly
