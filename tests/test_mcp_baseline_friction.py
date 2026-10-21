import json

import pytest
from jsonschema import ValidationError

from coalesce.graph import GraphError
from coalesce.server import TOOLS_GUIDE, _schema_for_action, schema_error_message
from coalesce.service import DocumentService


def test_saved_removed_standoff_field_is_structured_document_error(tmp_path):
    path = tmp_path / "old.coalesce.json"
    path.write_text(
        json.dumps(
            {
                "formatVersion": 1,
                "revision": 2,
                "graph": {
                    "nodes": [
                        {
                            "type": "Standoffs",
                            "id": "standoffs",
                            "target": "base",
                            "board": "board",
                            "selection": "explicit",
                            "hole_ids": [],
                            "minimum_diameter": 2,
                            "boss_diameter": 5,
                            "pilot_diameter": 2,
                        }
                    ]
                },
            }
        )
    )

    with pytest.raises(GraphError) as caught:
        DocumentService(path, warmup=False)

    assert caught.value.error["code"] == "INVALID_DOCUMENT"
    assert "minimumDiameter was removed" in caught.value.error["message"]


def test_bare_quantity_number_includes_expected_shape(tmp_path):
    service = DocumentService(tmp_path / "empty.coalesce.json", warmup=False)
    schema = _schema_for_action("feature", {"action": "create", "kind": "extrude"})
    arguments = {
        "action": "create",
        "expectedRevision": 0,
        "requestId": "bad-distance",
        "featureId": "part",
        "kind": "extrude",
        "parameters": {
            "profile": {"sketchId": "profile", "profileIds": ["outline"]},
            "distance": 2,
            "operation": "new_body",
            "direction": "normal",
        },
    }
    with pytest.raises(ValidationError) as raised:
        from jsonschema import validate

        validate(arguments, schema)
    error = schema_error_message(raised.value)
    assert '{"value": 2, "unit": "mm"}' in error
    assert "field: parameters.distance" in error
    del service


def test_export_guide_describes_feature_id_resolution():
    assert "exactly one terminal manufacturing part" in TOOLS_GUIDE
