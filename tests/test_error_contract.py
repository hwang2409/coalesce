import ast
import re
from pathlib import Path

import pytest
from test_mcp_cut_hole_circle import cut
from test_mcp_vertical import call_server_tool

import coalesce.service as service_module
from coalesce.graph import GraphError
from coalesce.service import DocumentService

ROOT = Path(__file__).parents[1]
UPPER_SNAKE = re.compile(r"^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*$")
ERROR_CONSTRUCTORS = {"GraphError", "RequestError", "BoardImportError", "ExportFailure"}


def documented_codes() -> set[str]:
    path = ROOT / "docs" / "errors.md"
    assert path.is_file(), "docs/errors.md must document the public error contract"
    return set(re.findall(r"^\| `([A-Z0-9_]+)` \|", path.read_text(), re.MULTILINE))


def raised_codes() -> list[tuple[Path, int, str]]:
    found = []
    for path in (ROOT / "src").rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            if node.func.id not in ERROR_CONSTRUCTORS or not node.args:
                continue
            code = node.args[0]
            assert isinstance(code, ast.Constant) and isinstance(code.value, str), (
                f"{path.relative_to(ROOT)}:{node.lineno} must use one literal stable error code"
            )
            found.append((path, node.lineno, code.value))
    return found


def assert_failure_shape(result, *, operation, revision, feature_id=None):
    assert result["ok"] is False
    assert result["revision"] == revision
    error = result["error"]
    assert {"code", "message", "operation", "retryable"} <= error.keys()
    assert error["operation"] == operation
    assert isinstance(error["retryable"], bool)
    assert "revision" not in error
    if feature_id is not None:
        assert error["featureId"] == feature_id
    if "affectedParts" in error:
        assert isinstance(error["affectedParts"], list)


def test_every_raised_error_code_is_documented_upper_snake():
    documented = documented_codes()
    raised = raised_codes()

    assert documented
    for path, line, code in raised:
        location = f"{path.relative_to(ROOT)}:{line}"
        assert UPPER_SNAKE.fullmatch(code), f"{location}: {code!r} is not UPPER_SNAKE"
        assert code in documented, f"{location}: {code} is not documented"


@pytest.mark.anyio
async def test_boolean_failure_has_public_shape_and_keeps_kernel_text_out_of_message(
    tmp_path, monkeypatch
):
    service = DocumentService(tmp_path / "boolean.coalesce.json")

    def fail_boolean(graph):
        raise GraphError(
            "BOOLEAN_FAILED",
            "the boolean operation could not produce valid geometry",
            feature="base",
            kernelMessage="BRep_API: command not done",
        )

    monkeypatch.setattr(service_module, "regenerate", fail_boolean)
    result = await call_server_tool(
        service,
        "sketch",
        {
            "action": "create",
            "expectedRevision": 0,
            "requestId": "boolean-failure",
            "sketchId": "profile",
            "plane": {"kind": "origin", "name": "XY"},
            "entities": [
                {
                    "id": "outline",
                    "kind": "rectangle",
                    "center": [0, 0],
                    "width": {"value": 10, "unit": "mm"},
                    "height": {"value": 10, "unit": "mm"},
                }
            ],
        },
    )

    assert_failure_shape(result, operation="sketch", revision=0, feature_id="base")
    assert result["error"]["code"] == "BOOLEAN_FAILED"
    assert "BRep" not in result["error"]["message"]
    assert result["error"]["kernelMessage"] == "BRep_API: command not done"


@pytest.mark.anyio
async def test_unknown_id_and_revision_conflict_have_recovery_shape(tmp_path):
    service = DocumentService(tmp_path / "recovery.coalesce.json")
    unknown = await call_server_tool(
        service,
        "feature",
        cut(
            request_id="missing-target",
            revision=0,
            feature_id="pocket",
            target="missing-body",
            tool="missing-tool",
        ),
    )
    assert_failure_shape(unknown, operation="feature", revision=0, feature_id="pocket")
    assert unknown["error"]["code"] == "UNKNOWN_ID"
    assert unknown["error"]["retryable"] is False

    conflict = await call_server_tool(
        service,
        "feature",
        cut(
            request_id="stale",
            revision=1,
            feature_id="pocket",
            target="missing-body",
            tool="missing-tool",
        ),
    )
    assert_failure_shape(conflict, operation="feature", revision=0, feature_id="pocket")
    assert conflict["error"]["code"] == "REVISION_CONFLICT"
    assert conflict["error"]["retryable"] is True
    assert conflict["error"]["expected"] == 1
    assert conflict["error"]["actual"] == 0


def test_error_document_is_machine_readable():
    path = ROOT / "docs" / "errors.md"
    assert path.is_file()
    text = path.read_text()
    assert "## error shape" in text
    assert "## families" in text
    assert all(f"| `{code}` |" in text for code in documented_codes())
