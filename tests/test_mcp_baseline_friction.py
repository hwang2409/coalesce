import json
import subprocess
import sys

import pytest
from jsonschema import ValidationError

from coalesce.graph import GraphError
from coalesce.server import TOOLS_GUIDE, _schema_for_action, schema_error_message
from coalesce.service import DocumentService


def _old_standoffs_document(path, key="minimum_diameter"):
    node = {
        "type": "Standoffs",
        "id": "standoffs",
        "target": "base",
        "board": "board",
        "selection": "explicit",
        "hole_ids": [],
        key: 2,
        "boss_diameter": 5,
        "pilot_diameter": 2,
    }
    path.write_text(json.dumps({"formatVersion": 1, "revision": 2, "graph": {"nodes": [node]}}))
    return path


def test_saved_removed_standoff_field_is_structured_document_error(tmp_path):
    path = _old_standoffs_document(tmp_path / "old.coalesce.json")

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


@pytest.mark.parametrize("key", ["minimum_diameter", "minimumDiameter"])
@pytest.mark.parametrize("viewer", [True, False])
def test_cli_reports_invalid_document_without_traceback(tmp_path, key, viewer):
    path = _old_standoffs_document(tmp_path / "old.coalesce.json", key)
    before = path.read_text()
    command = [sys.executable, "-m", "coalesce", "--document", str(path)]
    if not viewer:
        command.append("--no-viewer")
    result = subprocess.run(command, input="", capture_output=True, text=True, timeout=60, check=False)

    assert result.returncode == 2
    assert "Traceback" not in result.stderr
    assert "coalesce viewer:" not in result.stderr
    error = json.loads(result.stderr.strip().splitlines()[-1])["error"]
    assert error["code"] == "INVALID_DOCUMENT"
    assert "all_mounting" in error["message"]
    assert path.read_text() == before
