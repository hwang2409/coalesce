import math

import pytest
from test_mcp_assembly import occurrence_create, transform
from test_mcp_vertical import FEATURE, SKETCH, run_server

from coalesce.assembly import Assembly, Occurrence, RigidTransform
from coalesce.service import DocumentService


def _requirement(**extra):
    return {"id": "assembly-clearance", "kind": "assembly_interference", **extra}


def _placed_service(tmp_path):
    service = DocumentService(tmp_path / "assembly-requirements.coalesce.json", warmup=False)
    assert service.sketch(SKETCH)["ok"]
    assert service.feature(FEATURE)["ok"]
    return service


def test_real_overlap_reports_volume_ids_and_composes_with_valid_brep(tmp_path):
    service = _placed_service(tmp_path)
    assert service.feature(occurrence_create(2, "place-a", "a", 0))["ok"]
    assert service.feature(occurrence_create(3, "place-b", "b", 20))["ok"]

    response = service.check(
        {
            "action": "evaluate",
            "requirements": [
                _requirement(),
                {"id": "valid", "kind": "valid_brep"},
            ],
        }
    )

    assert response["gate"] == "FAIL"
    result = response["results"][0]
    assert result["status"] == "FAIL"
    assert result["measured"]["failedPairs"][0]["occurrenceIds"] == ["a", "b"]
    assert result["measured"]["failedPairs"][0]["overlapVolume"]["value"] > 0
    assert response["results"][1]["status"] == "PASS"


def test_clearance_threshold_fails_with_units_then_real_move_passes(tmp_path):
    service = _placed_service(tmp_path)
    assert service.feature(occurrence_create(2, "place-a", "a", 0))["ok"]
    assert service.feature(occurrence_create(3, "place-b", "b", 61))["ok"]
    request = {
        "action": "evaluate",
        "requirements": [_requirement(minimumClearance={"value": 2, "unit": "mm"})],
    }

    failed = service.check(request)
    assert failed["gate"] == "FAIL"
    result = failed["results"][0]
    assert result["status"] == "FAIL"
    assert result["measured"]["minimumClearance"] == pytest.approx({"value": 1, "unit": "mm"})
    assert result["threshold"]["minimumClearance"] == {"value": 2, "unit": "mm"}

    assert service.feature(
        {
            "action": "update",
            "kind": "assembly_occurrence",
            "expectedRevision": 4,
            "requestId": "increase-clearance",
            "occurrenceId": "b",
            "set": {"transform": transform(62)},
        }
    )["ok"]
    passed = service.check(request)
    assert passed["gate"] == "PASS"
    assert passed["results"][0]["status"] == "PASS"
    assert passed["results"][0]["measured"]["minimumClearance"] == pytest.approx(
        {"value": 2, "unit": "mm"}
    )


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("minimumClearance", math.inf, "NON_FINITE_VALUE"),
        ("minimumClearance", -1, "INVALID_PARAMETER"),
        ("maximumOverlapVolume", 1e309, "NON_FINITE_VALUE"),
        ("maximumOverlapVolume", -5e-324, "INVALID_PARAMETER"),
    ],
)
def test_threshold_numeric_extremes_fail_without_state_change(tmp_path, field, value, code):
    service = _placed_service(tmp_path)
    service.feature(occurrence_create(2, "place-a", "a", 0))
    service.feature(occurrence_create(3, "place-b", "b", 61))
    before = service.store.path.read_bytes()
    unit = "mm^3" if field == "maximumOverlapVolume" else "mm"

    result = service.check(
        {"action": "evaluate", "requirements": [_requirement(**{field: {"value": value, "unit": unit}})]}
    )

    assert result["error"]["code"] == code
    assert service.store.path.read_bytes() == before


@pytest.mark.parametrize(
    ("field", "value", "expected_gate"),
    [
        ("minimumClearance", 5e-324, "PASS"),
        ("minimumClearance", 1e308, "FAIL"),
        ("maximumOverlapVolume", 5e-324, "PASS"),
        ("maximumOverlapVolume", 1e308, "PASS"),
    ],
)
def test_finite_extreme_thresholds_are_evaluated(tmp_path, field, value, expected_gate):
    service = _placed_service(tmp_path)
    service.feature(occurrence_create(2, "place-a", "a", 0))
    service.feature(occurrence_create(3, "place-b", "b", 61))
    unit = "mm^3" if field == "maximumOverlapVolume" else "mm"

    result = service.check(
        {"action": "evaluate", "requirements": [_requirement(**{field: {"value": value, "unit": unit}})]}
    )

    assert result["gate"] == expected_gate


def test_omitted_ids_over_pair_cap_fails_clearly_and_explicit_subset_passes(tmp_path):
    service = _placed_service(tmp_path)
    revision = 2
    for index in range(16):
        result = service.feature(
            occurrence_create(revision, f"place-{index}", f"copy-{index:02}", index * 70)
        )
        assert result["ok"]
        revision += 1
    capped = service.check({"action": "evaluate", "requirements": [_requirement()]})
    assert capped["gate"] == "PASS"
    assert len(capped["results"][0]["measured"]["pairs"]) == 120

    assert service.feature(occurrence_create(revision, "place-16", "copy-16", 16 * 70))["ok"]
    failed = service.check({"action": "evaluate", "requirements": [_requirement()]})
    assert failed["error"]["code"] == "INVALID_PARAMETER"
    assert failed["error"]["maximumOccurrenceCount"] == 16
    assert failed["error"]["selectedOccurrenceCount"] == 17

    passed = service.check(
        {
            "action": "evaluate",
            "requirements": [_requirement(occurrenceIds=["copy-00", "copy-01"])],
        }
    )
    assert passed["gate"] == "PASS"


def test_total_pair_cap_applies_across_requirements_before_inspection(tmp_path):
    service = _placed_service(tmp_path)
    revision = 2
    occurrence_ids = []
    for index in range(12):
        occurrence_id = f"copy-{index:02}"
        occurrence_ids.append(occurrence_id)
        assert service.feature(
            occurrence_create(revision, f"place-{index}", occurrence_id, index * 70)
        )["ok"]
        revision += 1

    result = service.check(
        {
            "action": "evaluate",
            "requirements": [
                _requirement(id="first", occurrenceIds=occurrence_ids),
                _requirement(id="second", occurrenceIds=occurrence_ids),
            ],
        }
    )

    assert result["error"]["code"] == "INVALID_PARAMETER"
    assert result["error"]["totalPairCount"] == 132
    assert result["error"]["maximumPairCount"] == 120


def test_deadline_marks_unchecked_real_pairs_as_errors(tmp_path, monkeypatch):
    service = _placed_service(tmp_path)
    assert service.feature(occurrence_create(2, "place-a", "a", 0))["ok"]
    assert service.feature(occurrence_create(3, "place-b", "b", 20))["ok"]
    monkeypatch.setattr("coalesce.assembly_inspection.INSPECTION_DEADLINE_SECONDS", 0.0)

    response = service.check({"action": "evaluate", "requirements": [_requirement()]})

    assert response["gate"] == "ERROR"
    result = response["results"][0]
    assert result["status"] == "ERROR"
    assert result["measured"]["failedPairs"][0]["error"]["reason"] == "DEADLINE"


def test_exact_kernel_failure_makes_requirement_gate_error(tmp_path, monkeypatch):
    service = _placed_service(tmp_path)
    service.feature(occurrence_create(2, "place-a", "a", 0))
    service.feature(occurrence_create(3, "place-b", "b", 20))

    def fail(*_args):
        raise ValueError("forced exact common failure")

    monkeypatch.setattr("coalesce.assembly_inspection._common_volume", fail)
    result = service.check({"action": "evaluate", "requirements": [_requirement()]})

    assert result["gate"] == "ERROR"
    assert result["results"][0]["status"] == "ERROR"
    assert result["results"][0]["measured"]["failedPairs"][0]["error"]["code"] == "INSPECTION_FAILED"


def test_unknown_duplicate_and_stale_occurrences_use_structured_errors_without_mutation(tmp_path):
    service = _placed_service(tmp_path)
    service.feature(occurrence_create(2, "place-a", "a", 0))
    service.feature(occurrence_create(3, "place-b", "b", 50))
    before = service.store.path.read_bytes()

    unknown = service.check(
        {"action": "evaluate", "requirements": [_requirement(occurrenceIds=["a", "missing"])]}
    )
    assert unknown["error"]["code"] == "UNKNOWN_ID"
    duplicate = service.check(
        {"action": "evaluate", "requirements": [_requirement(occurrenceIds=["a", "a"])]}
    )
    assert duplicate["error"]["code"] == "INVALID_PARAMETER"
    assert service.store.path.read_bytes() == before

    stale = Occurrence("a", "missing/body", RigidTransform((0, 0, 0), (0, 0, 1), 0))
    service._document.assembly = Assembly({"a": stale, "b": service._document.assembly.occurrences["b"]})
    result = service.check({"action": "evaluate", "requirements": [_requirement()]})
    assert result["error"]["code"] == "ASSEMBLY_REFERENCE_STALE"
    assert result["error"]["occurrenceIds"] == ["a"]


@pytest.mark.anyio
async def test_real_stdio_two_occurrences_of_one_part_pass_and_clearance_failure_has_units(tmp_path):
    results = await run_server(
        tmp_path / "stdio-assembly-check.coalesce.json",
        [
            ("sketch", SKETCH),
            ("feature", FEATURE),
            ("feature", occurrence_create(2, "place-a", "a", 0)),
            ("feature", occurrence_create(3, "place-b", "b", 61)),
            (
                "check",
                {
                    "action": "evaluate",
                    "requirements": [_requirement(minimumClearance={"value": 2, "unit": "mm"})],
                },
            ),
        ],
    )
    result = results[-1]
    assert result["gate"] == "FAIL"
    assert result["results"][0]["measured"]["failedPairs"][0]["partIds"] == [
        "plate_extrude/outline",
        "plate_extrude/outline",
    ]
    assert result["results"][0]["measured"]["minimumClearance"]["unit"] == "mm"
