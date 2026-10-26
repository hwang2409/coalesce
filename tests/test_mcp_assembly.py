import math
import sys

import pytest
from test_mcp_vertical import FEATURE, SKETCH, run_server

from coalesce.service import DocumentService


def transform(x=0, axis=(0, 0, 1), angle=0, unit="deg"):
    return {
        "translation": {"value": [x, 0, 0], "unit": "mm"},
        "rotation": {"axis": list(axis), "angle": {"value": angle, "unit": unit}},
    }


def occurrence_create(revision, request_id, occurrence_id, x=0):
    return {
        "action": "create",
        "kind": "assembly_occurrence",
        "expectedRevision": revision,
        "requestId": request_id,
        "occurrenceId": occurrence_id,
        "parameters": {"partId": "plate_extrude/outline", "transform": transform(x)},
    }


@pytest.mark.anyio
async def test_real_stdio_occurrences_are_named_sorted_mutable_and_restart_exact(tmp_path):
    path = tmp_path / "assembly.coalesce.json"
    create_z = occurrence_create(2, "place-z", "z-copy", 20)
    create_a = occurrence_create(3, "place-a", "a-copy", 0)
    update_z = {
        "action": "update",
        "kind": "assembly_occurrence",
        "expectedRevision": 4,
        "requestId": "move-z",
        "occurrenceId": "z-copy",
        "set": {"transform": transform(10, (0, 0, 2), 450)},
    }
    first = await run_server(
        path,
        [
            ("sketch", SKETCH),
            ("feature", FEATURE),
            ("feature", create_z),
            ("feature", create_a),
            ("feature", update_z),
            ("query", {"select": {"kind": "assembly"}}),
            ("feature", update_z),
        ],
    )
    assert [row["occurrenceId"] for row in first[5]["occurrences"]] == ["a-copy", "z-copy"]
    assert [row["partId"] for row in first[5]["occurrences"]] == [
        "plate_extrude/outline",
        "plate_extrude/outline",
    ]
    assert first[5]["occurrences"][1]["bbox"]["center"]["x"] == pytest.approx(10)
    assert first[5]["occurrences"][1]["bbox"]["x"]["value"] == pytest.approx(40)
    assert first[5]["occurrences"][1]["bbox"]["y"]["value"] == pytest.approx(60)
    assert first[5]["occurrences"][1]["transform"] == {
        "translation": {"value": [10.0, 0.0, 0.0], "unit": "mm"},
        "rotation": {"axis": [0.0, 0.0, 1.0], "angle": {"value": 90.0, "unit": "deg"}},
    }
    assert first[6] == first[4]

    restarted = await run_server(
        path,
        [("query", {"select": {"kind": "assembly"}}), ("feature", update_z)],
    )
    assert restarted[0] == first[5]
    assert restarted[1] == first[4]

    deleted = await run_server(
        path,
        [
            (
                "feature",
                {
                    "action": "delete",
                    "kind": "assembly_occurrence",
                    "expectedRevision": 5,
                    "requestId": "delete-a",
                    "occurrenceId": "a-copy",
                },
            ),
            ("query", {"select": {"kind": "assembly"}}),
        ],
    )
    assert deleted[0]["occurrenceId"] == "a-copy"
    assert [row["occurrenceId"] for row in deleted[1]["occurrences"]] == ["z-copy"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("axis", "angle", "expected_axis", "expected_angle"),
    [
        ((sys.float_info.max,) * 3, 45, (1 / math.sqrt(3),) * 3, 45),
        ((5e-324, 0, 0), 45, (1, 0, 0), 45),
        ((0, 0, 1), 1e308, (0, 0, 1), -64),
        ((0, 0, 1), -1e308, (0, 0, 1), 64),
        ((0, 0, 1), 180, (0, 0, 1), -180),
        ((0, 0, 1), -180, (0, 0, 1), -180),
    ],
)
async def test_extreme_transform_create_query_save_and_restart(
    tmp_path, axis, angle, expected_axis, expected_angle
):
    path = tmp_path / f"transform-{angle}.coalesce.json"
    payload = occurrence_create(2, "place", "plate")
    payload["parameters"]["transform"] = transform(axis=axis, angle=angle)

    created = await run_server(
        path,
        [("sketch", SKETCH), ("feature", FEATURE), ("feature", payload), ("query", {"select": {"kind": "assembly"}})],
    )
    assert created[2]["ok"] is True
    stored = created[3]["occurrences"][0]["transform"]["rotation"]
    assert stored["axis"] == pytest.approx(expected_axis)
    assert stored["angle"]["value"] == expected_angle

    restarted = await run_server(path, [("query", {"select": {"kind": "assembly"}})])
    assert restarted[0] == created[3]


def test_invalid_placement_is_rejected_before_save(tmp_path, monkeypatch):
    path = tmp_path / "invalid-placement.coalesce.json"
    service = DocumentService(path, warmup=False)
    assert service.sketch(SKETCH)["ok"]
    assert service.feature(FEATURE)["ok"]
    before = path.read_bytes()

    def fail_placement(*_args):
        raise ValueError("invalid placement")

    monkeypatch.setattr("coalesce.assembly.locate_shape", fail_placement)
    result = service.feature(occurrence_create(2, "place", "plate"))

    assert result["ok"] is False
    assert result["error"]["code"] == "KERNEL_OPERATION_FAILED"
    assert path.read_bytes() == before


def test_stale_part_delete_fails_atomically_and_names_occurrence(tmp_path):
    path = tmp_path / "atomic.coalesce.json"
    service = DocumentService(path, warmup=False)
    assert service.sketch(SKETCH)["ok"]
    assert service.feature(FEATURE)["ok"]
    assert service.feature(occurrence_create(2, "place", "plate-at-origin"))["ok"]
    before = path.read_bytes()

    result = service.sketch(
        {
            "action": "delete",
            "expectedRevision": 3,
            "requestId": "remove-part",
            "sketchId": "plate_profile",
            "cascade": True,
        }
    )

    assert result["ok"] is False
    assert result["revision"] == 3
    assert result["error"] == {
        **result["error"],
        "code": "ASSEMBLY_REFERENCE_STALE",
        "occurrenceIds": ["plate-at-origin"],
        "partIds": ["plate_extrude/outline"],
        "reason": "EMPTY",
    }
    assert path.read_bytes() == before


def test_graph_edits_preserve_placement_and_part_id_changes_fail_atomically(tmp_path):
    path = tmp_path / "edits.coalesce.json"
    service = DocumentService(path, warmup=False)
    service.sketch(SKETCH)
    implicit_feature = {
        **FEATURE,
        "parameters": {
            **FEATURE["parameters"],
            "profile": {"sketchId": "plate_profile"},
        },
    }
    service.feature(implicit_feature)
    service.feature(occurrence_create(2, "place", "plate"))
    original = service.query({"select": {"kind": "assembly"}})["occurrences"][0]

    moved = service.sketch(
        {
            "action": "update",
            "expectedRevision": 3,
            "requestId": "move-profile",
            "sketchId": "plate_profile",
            "set": {"position": [5, 0, 0]},
        }
    )
    assert moved["ok"]
    after_move = service.query({"select": {"kind": "assembly"}})["occurrences"][0]
    assert after_move["occurrenceId"] == original["occurrenceId"]
    assert after_move["transform"] == original["transform"]

    cutter = {
        "action": "create",
        "expectedRevision": 4,
        "requestId": "cutter-sketch",
        "sketchId": "cutter",
        "plane": {"kind": "origin", "name": "XY"},
        "entities": [
            {
                "id": "opening",
                "kind": "circle",
                "center": [5, 0],
                "radius": {"value": 2, "unit": "mm"},
            }
        ],
    }
    assert service.sketch(cutter)["ok"]
    cut = {
        "action": "create",
        "kind": "cut",
        "expectedRevision": 5,
        "requestId": "downstream-cut",
        "featureId": "opening-cut",
        "parameters": {
            "target": {"featureId": "plate_extrude"},
            "tool": {"sketchId": "cutter"},
            "depth": {"kind": "through_all"},
            "flip": False,
        },
    }
    assert service.feature(cut)["ok"]
    after_cut = service.query({"select": {"kind": "assembly"}})["occurrences"][0]
    assert after_cut["terminalFeatureId"] == "opening-cut"
    assert after_cut["transform"] == original["transform"]

    splitter = {
        "action": "create",
        "expectedRevision": 6,
        "requestId": "splitter-sketch",
        "sketchId": "splitter",
        "plane": {"kind": "origin", "name": "XY"},
        "entities": [
            {
                "id": "strip",
                "kind": "rectangle",
                "center": [5, 0],
                "width": {"value": 2, "unit": "mm"},
                "height": {"value": 50, "unit": "mm"},
            }
        ],
    }
    assert service.sketch(splitter)["ok"]
    before = path.read_bytes()
    split = service.feature(
        {
            "action": "create",
            "kind": "cut",
            "expectedRevision": 7,
            "requestId": "split-part",
            "featureId": "split-cut",
            "parameters": {
                "target": {"featureId": "opening-cut"},
                "tool": {"sketchId": "splitter"},
                "depth": {"kind": "through_all"},
                "flip": False,
            },
        }
    )
    assert split["error"]["code"] == "ASSEMBLY_REFERENCE_STALE"
    assert split["error"]["occurrenceIds"] == ["plate"]
    assert split["error"]["partIds"] == ["plate_extrude/outline"]
    assert split["error"]["reason"] == "SPLIT"
    assert path.read_bytes() == before


def test_occurrence_limits_unknown_parts_and_revision_contract(tmp_path):
    path = tmp_path / "limits.coalesce.json"
    service = DocumentService(path, warmup=False)
    service.sketch(SKETCH)
    service.feature(FEATURE)

    unknown = occurrence_create(2, "unknown", "unknown")
    unknown["parameters"]["partId"] = "missing/body"
    result = service.feature(unknown)
    assert result["error"]["code"] == "UNKNOWN_ID"
    assert result["revision"] == 2

    first = service.feature(occurrence_create(2, "first", "copy-00"))
    assert first["revision"] == 3
    duplicate = service.feature(occurrence_create(3, "duplicate", "copy-00"))
    assert duplicate["error"]["code"] == "DUPLICATE_ID"
    wrong = service.feature(occurrence_create(2, "wrong-revision", "copy-wrong"))
    assert wrong["error"]["code"] == "REVISION_CONFLICT"
    reused = service.feature(occurrence_create(3, "first", "copy-other"))
    assert reused["error"]["code"] == "REQUEST_ID_REUSED"

    for index in range(1, 64):
        result = service.feature(occurrence_create(2 + index, f"place-{index}", f"copy-{index:02}"))
        assert result["ok"]
    before = path.read_bytes()
    too_many = service.feature(occurrence_create(66, "place-64", "copy-64"))
    assert too_many["error"]["code"] == "ASSEMBLY_OCCURRENCE_LIMIT"
    assert too_many["error"]["limit"] == 64
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    ("bad_transform", "code", "message"),
    [
        (transform(axis=(0, 0, 0), angle=1), "INVALID_PARAMETER", "rotation axis must be non-zero"),
        (transform(x=float("inf")), "NON_FINITE_VALUE", "translation values must be finite"),
    ],
)
def test_invalid_transform_does_not_mutate(tmp_path, bad_transform, code, message):
    path = tmp_path / "invalid-transform.coalesce.json"
    service = DocumentService(path, warmup=False)
    service.sketch(SKETCH)
    service.feature(FEATURE)
    before = path.read_bytes()
    payload = occurrence_create(2, f"bad-{code}", "bad")
    payload["parameters"]["transform"] = bad_transform

    result = service.feature(payload)

    assert result["error"]["code"] == code
    assert result["error"]["message"] == message
    assert result["revision"] == 2
    assert path.read_bytes() == before


@pytest.mark.anyio
async def test_malformed_transform_is_structured_invalid_parameter(tmp_path):
    service = DocumentService(tmp_path / "malformed.coalesce.json", warmup=False)
    service.sketch(SKETCH)
    service.feature(FEATURE)
    payload = occurrence_create(2, "bad", "bad")
    payload["parameters"]["transform"]["rotation"] = {"axis": [0, 0], "angle": {"value": 0, "unit": "deg"}}

    from test_mcp_vertical import call_server_tool

    result = await call_server_tool(service, "feature", payload)
    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_PARAMETER"
    assert result["error"]["occurrenceId"] == "bad"
    assert "parameters.transform.rotation.axis" in result["error"]["message"]
    assert result["revision"] == 2


def ground_create(revision, request_id, mate_id, occurrence_id):
    return {
        "action": "create",
        "kind": "assembly_mate",
        "expectedRevision": revision,
        "requestId": request_id,
        "mateId": mate_id,
        "parameters": {"kind": "ground", "occurrenceId": occurrence_id},
    }


@pytest.mark.anyio
async def test_real_stdio_ground_create_update_delete_replay_and_restart(tmp_path):
    path = tmp_path / "ground.coalesce.json"
    create_a = occurrence_create(2, "place-a", "a")
    create_b = occurrence_create(3, "place-b", "b", 20)
    ground = ground_create(4, "ground-a", "fixture", "a")
    update = {
        "action": "update",
        "kind": "assembly_mate",
        "expectedRevision": 5,
        "requestId": "ground-b",
        "mateId": "fixture",
        "set": {"occurrenceId": "b"},
    }
    first = await run_server(
        path,
        [
            ("sketch", SKETCH),
            ("feature", FEATURE),
            ("feature", create_a),
            ("feature", create_b),
            ("feature", ground),
            ("feature", ground),
            ("feature", update),
            ("query", {"select": {"kind": "assembly"}}),
        ],
    )
    assert first[5] == first[4]
    assert first[7]["solveStatus"] == "UNDER_CONSTRAINED"
    assert first[7]["underConstrainedOccurrenceIds"] == ["a"]
    assert first[7]["mates"] == [{"mateId": "fixture", "kind": "ground", "occurrenceId": "b"}]
    by_id = {row["occurrenceId"]: row for row in first[7]["occurrences"]}
    assert (by_id["a"]["grounded"], by_id["a"]["remainingDof"]) == (False, 6)
    assert (by_id["b"]["grounded"], by_id["b"]["remainingDof"]) == (True, 0)
    assert by_id["a"]["mateIds"] == []
    assert by_id["b"]["mateIds"] == ["fixture"]

    restarted = await run_server(path, [("query", {"select": {"kind": "assembly"}})])
    assert restarted[0] == first[7]

    deleted = await run_server(
        path,
        [
            (
                "feature",
                {
                    "action": "delete",
                    "kind": "assembly_mate",
                    "expectedRevision": 6,
                    "requestId": "unground",
                    "mateId": "fixture",
                },
            ),
            ("query", {"select": {"kind": "assembly"}}),
        ],
    )
    assert deleted[0]["mateId"] == "fixture"
    assert deleted[1]["underConstrainedOccurrenceIds"] == ["a", "b"]


def test_duplicate_mate_and_second_ground_fail_atomically(tmp_path):
    path = tmp_path / "ground-atomic.coalesce.json"
    service = DocumentService(path, warmup=False)
    assert service.sketch(SKETCH)["ok"]
    assert service.feature(FEATURE)["ok"]
    assert service.feature(occurrence_create(2, "place", "plate"))["ok"]
    assert service.feature(ground_create(3, "ground", "fixture", "plate"))["ok"]
    before = path.read_bytes()

    duplicate = service.feature(ground_create(4, "duplicate", "fixture", "plate"))
    assert duplicate["error"]["code"] == "DUPLICATE_ID"
    assert path.read_bytes() == before

    conflict = service.feature(ground_create(4, "second", "fixture-2", "plate"))
    assert conflict["error"]["code"] == "MATE_CONFLICT"
    assert conflict["error"]["mateIds"] == ["fixture", "fixture-2"]
    assert conflict["error"]["occurrenceIds"] == ["plate"]
    assert path.read_bytes() == before

    delete_occurrence = service.feature(
        {
            "action": "delete",
            "kind": "assembly_occurrence",
            "expectedRevision": 4,
            "requestId": "delete-grounded",
            "occurrenceId": "plate",
        }
    )
    assert delete_occurrence["error"]["code"] == "HAS_DEPENDENTS"
    assert delete_occurrence["error"]["mateIds"] == ["fixture"]
    assert path.read_bytes() == before
