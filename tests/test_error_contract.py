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
VIEWER_CODE_SET = re.compile(
    r"const [A-Z_]+_CODES = new Set\(\[(.*?)\]\);", re.DOTALL
)


def documented_codes() -> set[str]:
    return set(documented_families())


def documented_families() -> dict[str, str]:
    path = ROOT / "docs" / "errors.md"
    assert path.is_file(), "docs/errors.md must document the public error contract"
    return dict(
        re.findall(r"^\| `([A-Z0-9_]+)` \| ([a-z]+) \|", path.read_text(), re.MULTILINE)
    )


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


def test_registry_is_the_exact_documented_public_contract():
    registry = getattr(service_module, "ERROR_REGISTRY", {})

    assert {code: definition.family for code, definition in registry.items()} == documented_families()
    assert all(UPPER_SNAKE.fullmatch(code) for code in registry)
    assert {code for code, definition in registry.items() if definition.retryable} == {
        "REVISION_CONFLICT"
    }


def test_viewer_code_sets_are_registered():
    registry = getattr(service_module, "ERROR_REGISTRY", {})
    viewer = (ROOT / "src" / "coalesce" / "viewer" / "static" / "viewer.js").read_text()
    code_sets = [set(re.findall(r"'([A-Z0-9_]+)'", body)) for body in VIEWER_CODE_SET.findall(viewer)]

    assert code_sets
    assert all(codes <= set(registry) for codes in code_sets)


def test_safe_error_rejects_unregistered_public_code():
    with pytest.raises(ValueError, match="UNDOCUMENTED_PROBE"):
        service_module._safe_error(
            0,
            None,
            "UNDOCUMENTED_PROBE",
            "review probe",
            operation="test",
        )


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
async def test_mutation_failures_keep_their_operation_id(tmp_path):
    service = DocumentService(tmp_path / "mutation-ids.coalesce.json")

    sketch = await call_server_tool(
        service,
        "sketch",
        {
            "action": "create",
            "expectedRevision": 0,
            "requestId": "invalid-sketch",
            "sketchId": "known-sketch",
            "plane": {"kind": "origin", "name": "XY"},
            "entities": [],
        },
    )
    imported = await call_server_tool(
        service,
        "import",
        {
            "action": "create",
            "expectedRevision": 0,
            "requestId": "invalid-import",
            "assetId": "known-asset",
            "format": "kicad_pcb",
        },
    )

    assert sketch["error"]["sketchId"] == "known-sketch"
    assert imported["error"]["assetId"] == "known-asset"


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


# Positional index of the code argument for every public error builder.
ERROR_BUILDERS = {
    "_safe_error": 2,
    "error_response": 2,
    "RequestError": 0,
    "GraphError": 0,
    "BoardImportError": 0,
    "ExportFailure": 0,
}
# Non-literal codes that only pass on a code already checked elsewhere:
# builder internals, re-raises of a caught error, and the fillet/chamfer table below.
PASS_THROUGH_CODES = {
    ("src/coalesce/edge_treatment.py", "GraphError", "failed_code"),
    ("src/coalesce/edge_treatment.py", "GraphError", "invalid_code"),
    ("src/coalesce/graph.py", "dict", "code"),
    ("src/coalesce/service.py", "RequestError", "error['code']"),
    ("src/coalesce/service.py", "RequestError", "error.code"),
    ("src/coalesce/service.py", "RequestError", "failure.code"),
    ("src/coalesce/service.py", "_safe_error", "code"),
    ("src/coalesce/service.py", "_safe_error", "error.code"),
    ("src/coalesce/service.py", "_safe_error", "failure.code"),
    ("src/coalesce/service.py", "dict", "code"),
    ("src/coalesce/service.py", "error_response", "code"),
    ("src/coalesce/service.py", "error_response", "error.code"),
    ("src/coalesce/server.py", "error_response", "code"),
}


def _builder_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _code_argument(call: ast.Call, index: int) -> ast.expr | None:
    keyword = next((item.value for item in call.keywords if item.arg == "code"), None)
    if keyword is not None:
        return keyword
    return call.args[index] if len(call.args) > index else None


def emitted_codes(root: Path = ROOT) -> tuple[list[tuple[str, int, str]], set[tuple[str, str, str]]]:
    """Every code literal given to a public error builder or a ``"code"`` dict key."""
    literals: list[tuple[str, int, str]] = []
    dynamic: set[tuple[str, str, str]] = set()

    def record(rel: str, line: int, kind: str, value: ast.expr) -> None:
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            literals.append((rel, line, value.value))
        else:
            dynamic.add((rel, kind, ast.unparse(value)))

    for path in sorted((root / "src" / "coalesce").rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call) and _builder_name(node) in ERROR_BUILDERS:
                name = _builder_name(node)
                value = _code_argument(node, ERROR_BUILDERS[name])
                if value is not None:
                    record(rel, node.lineno, name, value)
            elif isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values, strict=True):
                    if isinstance(key, ast.Constant) and key.value == "code":
                        record(rel, node.lineno, "dict", value)
    return literals, dynamic


def edge_treatment_table_codes(root: Path = ROOT) -> set[str]:
    source = (root / "src" / "coalesce" / "edge_treatment.py").read_text()
    codes = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Assign) and ast.unparse(node.targets[0]) == "(invalid_code, failed_code)":
            codes |= {
                item.value for item in ast.walk(node.value)
                if isinstance(item, ast.Constant) and isinstance(item.value, str) and item.value.isupper()
            }
    return codes


def documented_warnings() -> set[str]:
    text = (ROOT / "docs" / "errors.md").read_text().split("## inspection warnings", 1)[-1]
    return set(re.findall(r"^\| `([A-Z0-9_]+)` \|", text, re.MULTILINE))


def test_every_emitted_code_is_registered():
    from coalesce.errors import WARNING_CODES

    literals, dynamic = emitted_codes()
    known = set(service_module.ERROR_REGISTRY) | WARNING_CODES

    assert literals
    assert [item for item in literals if item[2] not in known] == []
    assert dynamic == PASS_THROUGH_CODES
    table = edge_treatment_table_codes()
    assert table == {"INVALID_FILLET_RADIUS", "FILLET_FAILED", "INVALID_CHAMFER_DISTANCE", "CHAMFER_FAILED"}
    assert table <= set(service_module.ERROR_REGISTRY)


def test_inspection_warning_codes_are_documented():
    from coalesce.errors import WARNING_CODES

    assert documented_warnings() == set(WARNING_CODES)
    assert not WARNING_CODES & set(service_module.ERROR_REGISTRY)
