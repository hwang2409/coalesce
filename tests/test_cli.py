import json
import subprocess
import sys

import pytest

from coalesce.__main__ import _viewer_port, main


@pytest.mark.parametrize(
    ("document_kind", "reason", "message", "location"),
    [
        (
            "malformed",
            "json_syntax",
            "document contains invalid JSON",
            {"line": 1, "column": 2},
        ),
        ("directory", "is_directory", "document path is a directory", {}),
    ],
)
def test_cli_reports_structured_document_load_errors(
    document_kind, reason, message, location, tmp_path
):
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

    assert result.returncode == 2
    assert "Traceback" not in result.stderr
    failure = json.loads(result.stderr)
    assert failure["error"] == {
        "code": "INVALID_DOCUMENT",
        "message": message,
        "retryable": False,
        "operation": "load",
        "reason": reason,
        **location,
    }


def test_oserror_after_document_load_is_not_mapped_to_invalid_document(tmp_path, monkeypatch):
    async def fail_after_startup(service):
        raise OSError("stdio failed")

    monkeypatch.setattr("coalesce.__main__.run_stdio", fail_after_startup)
    monkeypatch.setattr(
        sys,
        "argv",
        ["coalesce", "--document", str(tmp_path / "document.coalesce.json"), "--no-viewer"],
    )

    with pytest.raises(OSError, match="stdio failed"):
        main()



@pytest.mark.parametrize("value", ["0", "65535"])
def test_viewer_port_accepts_socket_range(value):
    assert _viewer_port(value) == int(value)


@pytest.mark.parametrize("value", ["-1", "65536", "not-a-port"])
def test_viewer_port_rejects_invalid_values_without_traceback(value):
    completed = subprocess.run(
        [sys.executable, "-m", "coalesce", "--viewer-port", value],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 2
    assert "usage:" in completed.stderr
    assert "traceback" not in completed.stderr.lower()
