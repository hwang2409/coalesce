import json
import subprocess
import sys

import pytest


@pytest.mark.parametrize("document_kind", ["malformed", "directory"])
def test_cli_reports_structured_document_load_errors(document_kind, tmp_path):
    document = tmp_path / "document.coalesce.json"
    if document_kind == "malformed":
        document.write_text("{")
    else:
        document.mkdir()

    result = subprocess.run(
        [sys.executable, "-m", "coalesce", "--document", str(document), "--no-viewer"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode != 0
    assert "Traceback" not in result.stderr
    assert json.loads(result.stderr) == {
        "ok": False,
        "revision": 0,
        "requestId": None,
        "units": "mm",
        "error": {
            "code": "INVALID_DOCUMENT",
            "message": "document could not be loaded",
            "retryable": False,
            "operation": "load",
        },
    }
