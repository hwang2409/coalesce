from coalesce.references import (
    Candidate,
    Cardinality,
    ElementKey,
    MaxAlong,
    Normal,
    Position,
    Reference,
    Status,
    resolve,
)


def candidate(role: str, source: str, center=(0, 0, 0), normal=(0, 0, 1)) -> Candidate:
    return Candidate(
        ElementKey("base", role, source),
        descriptor={"center": center, "normal": normal},
    )


def test_cardinality_reports_split_with_structured_candidates():
    pieces = [candidate("cap_end", "top"), candidate("cap_end", "top")]

    resolution = resolve(Reference("base", "cap_end"), {"base": pieces})

    assert resolution.status is Status.SPLIT
    assert resolution.error == {
        "feature": "base",
        "reference": {
            "feature": "base",
            "role": "cap_end",
            "cardinality": "one",
            "count": None,
            "source": None,
            "filters": [],
        },
        "status": "SPLIT",
        "candidates": [
            {
                "keys": [{"feature": "base", "role": "cap_end", "source": "top"}],
                "descriptor": {"center": (0, 0, 0), "normal": (0, 0, 1)},
            },
            {
                "keys": [{"feature": "base", "role": "cap_end", "source": "top"}],
                "descriptor": {"center": (0, 0, 0), "normal": (0, 0, 1)},
            },
        ],
        "suggestion": "use one_or_more or add a provenance-set filter",
    }
    assert (
        resolve(
            Reference("base", "cap_end", Cardinality.ONE_OR_MORE),
            {"base": pieces},
        ).status
        is Status.OK
    )


def test_filters_only_reduce_the_selected_provenance_set():
    candidates = {
        "base": [
            candidate("side", "left", (-5, 0, 0), (-1, 0, 0)),
            candidate("side", "right", (5, 0, 0), (1, 0, 0)),
            candidate("other", "outside", (100, 0, 0), (1, 0, 0)),
        ]
    }
    ref = Reference(
        "base",
        "side",
        filters=[Normal((1, 0, 0)), Position((1, 0, 0), 5), MaxAlong((1, 0, 0))],
    )

    resolution = resolve(ref, candidates)

    assert resolution.status is Status.OK
    assert resolution.candidates[0].key.source == "right"


def test_exact_cardinality_and_merged_status_are_explicit():
    pieces = [candidate("cap_end", "top"), candidate("cap_end", "top")]
    exact = resolve(
        Reference("base", "cap_end", Cardinality.EXACTLY, count=2),
        {"base": pieces},
    )
    merged = candidate("cap_end", "top")
    merged.merged_from = (ElementKey("boss", "cap_end", "boss_top"),)

    merged_resolution = resolve(Reference("base", "cap_end"), {"base": [merged]})

    assert exact.status is Status.OK
    assert merged_resolution.status is Status.MERGED
    assert merged_resolution.error["candidates"][0]["keys"] == [
        {"feature": "base", "role": "cap_end", "source": "top"},
        {"feature": "boss", "role": "cap_end", "source": "boss_top"},
    ]


def test_equal_maximum_is_ambiguous_instead_of_picking_by_index():
    candidates = {
        "base": [
            candidate("cap_end", "a", (0, 0, 10)),
            candidate("cap_end", "b", (5, 0, 10)),
        ]
    }

    resolution = resolve(
        Reference("base", "cap_end", filters=[MaxAlong((0, 0, 1))]), candidates
    )

    assert resolution.status is Status.AMBIGUOUS
    assert len(resolution.candidates) == 2


def test_missing_and_out_of_order_references_are_explicit():
    missing = resolve(Reference("gone", "cap_end"), {})
    ordered = resolve(
        Reference("later", "cap_end"),
        {},
        ordered_features=["consumer", "later"],
        before_feature="consumer",
    )

    assert missing.status is Status.EMPTY
    assert missing.error["feature"] == "gone"
    assert ordered.status is Status.ORDER
    assert ordered.error["suggestion"] == "move the referenced feature before its consumer"
