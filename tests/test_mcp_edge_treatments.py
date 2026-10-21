import pytest
from test_mcp_cut_hole_circle import extrude, rectangle_sketch
from test_mcp_vertical import run_server


def treatment(action, revision, request_id, feature_id, kind=None, *, size=None):
    payload = {
        "action": action,
        "expectedRevision": revision,
        "requestId": request_id,
        "featureId": feature_id,
    }
    if kind is not None:
        payload["kind"] = kind
    if action == "create":
        payload["parameters"] = {
            "target": {"featureId": "box"},
            "edges": [
                {
                    "feature": "box",
                    "role": f"between(side({side}),cap_end)",
                    "cardinality": "one",
                }
                for side in ("bottom", "right", "top", "left")
            ],
            "radius" if kind == "fillet" else "distance": {"value": size, "unit": "mm"},
        }
    elif action == "update":
        payload["set"] = {
            "radius" if kind == "fillet" else "distance": {"value": size, "unit": "mm"}
        }
    return payload


def box_calls():
    return [
        (
            "sketch",
            rectangle_sketch(
                request_id="box-sketch",
                revision=0,
                sketch_id="box_sketch",
                entity_id="outline",
                width=20,
                height=10,
            ),
        ),
        (
            "feature",
            extrude(
                request_id="box-extrude",
                revision=1,
                feature_id="box",
                sketch_id="box_sketch",
                profile_ids=["outline"],
                distance=5,
            ),
        ),
    ]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("kind", "initial", "updated"), [("fillet", 0.5, 0.75), ("chamfer", 1, 0.5)]
)
async def test_public_edge_treatment_create_update_restart_and_delete(
    tmp_path, kind, initial, updated
):
    path = tmp_path / f"{kind}.coalesce.json"
    created = await run_server(
        path,
        [
            *box_calls(),
            ("feature", treatment("create", 2, f"create-{kind}", "finish", kind, size=initial)),
            ("query", {"select": {"kind": "feature", "id": "finish"}}),
            ("query", {"select": {"kind": "measurements"}}),
        ],
    )

    assert created[2]["ok"] is True and created[2]["revision"] == 3
    assert created[3]["feature"]["type"] == kind.title()
    assert created[3]["feature"]["edges"][0]["feature"] == "box"
    assert len(created[3]["edgeTreatment"]["sources"]) == 4
    assert len(created[3]["edgeTreatment"]["generatedFaces"]) == 4
    assert all(
        face["role"].startswith("blend(" if kind == "fillet" else "chamfer(")
        for face in created[3]["edgeTreatment"]["generatedFaces"]
    )
    before_volume = created[4]["volume"]["value"]

    restarted = await run_server(
        path,
        [
            ("query", {"select": {"kind": "measurements"}}),
            ("feature", treatment("update", 3, f"update-{kind}", "finish", kind, size=updated)),
            ("query", {"select": {"kind": "measurements"}}),
            ("feature", treatment("delete", 4, f"delete-{kind}", "finish")),
            ("query", {"select": {"kind": "measurements"}}),
        ],
    )

    assert restarted[0]["volume"]["value"] == before_volume
    assert restarted[1]["ok"] is True and restarted[1]["revision"] == 4
    assert restarted[2]["volume"]["value"] != before_volume
    assert restarted[3]["ok"] is True and restarted[3]["revision"] == 5
    assert restarted[4]["volume"]["unit"] == "mm^3"
    assert restarted[4]["volume"]["value"] == pytest.approx(1000)


@pytest.mark.anyio
async def test_public_chamfer_reference_survives_base_dimension_update(tmp_path):
    path = tmp_path / "chamfer-edit.coalesce.json"
    results = await run_server(
        path,
        [
            *box_calls(),
            ("feature", treatment("create", 2, "create-chamfer", "bevel", "chamfer", size=1)),
            (
                "sketch",
                {
                    "action": "update",
                    "expectedRevision": 3,
                    "requestId": "widen-box",
                    "sketchId": "box_sketch",
                    "set": {
                        "entities": [
                            {
                                "id": "outline",
                                "kind": "rectangle",
                                "center": [0, 0],
                                "width": {"value": 30, "unit": "mm"},
                                "height": {"value": 10, "unit": "mm"},
                            }
                        ]
                    },
                },
            ),
            ("query", {"select": {"kind": "measurements"}}),
        ],
    )

    assert results[3]["ok"] is True and results[3]["revision"] == 4
    assert results[4]["parts"][0]["terminalFeatureId"] == "bevel"
    assert results[4]["volume"]["value"] < 1500


@pytest.mark.anyio
async def test_chamfered_part_step_export_reloads_as_one_valid_solid(tmp_path):
    path = tmp_path / "chamfer-export.coalesce.json"
    target = tmp_path / "chamfer.step"
    results = await run_server(
        path,
        [
            *box_calls(),
            ("feature", treatment("create", 2, "create-chamfer", "bevel", "chamfer", size=1)),
            ("export", {"expectedRevision": 3, "path": str(target)}),
        ],
    )

    exported = results[3]
    assert exported["ok"] is True
    assert exported["partIds"] == ["box/outline"]
    assert exported["validation"]["geometry"] == {"status": "passed", "freshProcess": True}
    assert exported["validation"]["reimport"]["partCount"] == 1
    assert exported["validation"]["reimport"]["solidCount"] == 1


@pytest.mark.anyio
async def test_public_oversized_chamfer_error_is_atomic(tmp_path):
    path = tmp_path / "oversized.coalesce.json"
    results = await run_server(
        path,
        [
            *box_calls(),
            ("feature", treatment("create", 2, "oversized", "bevel", "chamfer", size=100)),
            ("query", {"select": {"kind": "graph"}}),
        ],
    )

    error = results[2]
    assert error["ok"] is False and error["revision"] == 2
    assert error["error"]["feature"] == "bevel"
    assert error["error"]["distance"] == 100
    assert len(error["error"]["edges"]) == 4
    assert error["error"]["safeRange"] == "unknown"
    assert {node["id"] for node in results[3]["graph"]["nodes"]} == {"box_sketch", "box"}
