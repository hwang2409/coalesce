import pytest
from test_mcp_vertical import call_server_tool
from test_pcb_enclosure import (
    FEATURE_HOLES,
    FIXTURES,
    base_request,
    import_request,
    quantity,
    standoffs_request,
)
from test_pcb_lid import lid_request
from test_port_cutout import explicit_envelope, port_request

from coalesce.service import DocumentService


async def _call(service, name, request):
    return await call_server_tool(service, name, request)


async def _setup(service, *, standoffs=True, lid=False):
    assert (await _call(service, "import", import_request(FIXTURES / "features.kicad_pcb")))[
        "ok"
    ]
    assert (await _call(service, "feature", base_request()))["ok"]
    revision = 2
    if standoffs:
        assert (await _call(service, "feature", standoffs_request()))["ok"]
        revision += 1
    if lid:
        assert (await _call(service, "feature", lid_request(revision=revision)))["ok"]
        revision += 1
    return revision


async def _assert_mounting_holes_remain_covered(service):
    result = await _call(service, "query", {"select": {"kind": "enclosure", "base": "base"}})
    assert result["ok"] is True
    assert result["standoffs"]["coveredHoleIds"] == FEATURE_HOLES


@pytest.mark.anyio
async def test_standoff_wall_conflict_limit_is_safe_when_applied(tmp_path):
    service = DocumentService(tmp_path / "standoff-limit.coalesce.json")
    await _setup(service, standoffs=False)

    failed = await _call(
        service,
        "feature",
        standoffs_request(bossDiameter=quantity(7)),
    )

    assert failed["ok"] is False
    assert failed["error"]["code"] == "INVALID_PARAMETER"
    limits = failed["error"]["limits"]
    assert limits["parameter"] == "bossDiameter"
    assert limits["current"] == 7
    assert limits["minimum"] == 1.6
    assert limits["maximum"] < 7
    assert limits["unit"] == "mm"

    repaired = await _call(
        service,
        "feature",
        standoffs_request(
            request_id="safe-standoffs",
            bossDiameter=quantity(limits["suggested"]),
        ),
    )
    assert repaired["ok"] is True
    await _assert_mounting_holes_remain_covered(service)


@pytest.mark.anyio
async def test_harness_left_port_collision_offers_safe_wall_alternative(tmp_path):
    service = DocumentService(tmp_path / "port-standoff-alternative.coalesce.json")
    await _setup(service)

    failed = await _call(
        service,
        "feature",
        port_request(
            revision=3,
            wall="left",
            envelope=explicit_envelope(width=4, height=2, bottom=-1),
        ),
    )

    assert failed["ok"] is False
    assert failed["error"]["code"] == "INVALID_PARAMETER"
    assert failed["error"]["conflict"] in FEATURE_HOLES
    alternative = failed["error"]["alternatives"][0]
    assert alternative["set"]["wall"] in {"right", "front", "back"}

    repaired_request = port_request(
        revision=3,
        request_id="safe-port-wall",
        wall=alternative["set"]["wall"],
        envelope=explicit_envelope(width=4, height=2, bottom=-1),
    )
    repaired = await _call(service, "feature", repaired_request)
    assert repaired["ok"] is True
    await _assert_mounting_holes_remain_covered(service)


@pytest.mark.anyio
async def test_port_lid_conflict_offers_safe_envelope_alternative(tmp_path):
    service = DocumentService(tmp_path / "port-lid-alternative.coalesce.json")
    await _setup(service, lid=True)

    failed = await _call(service, "feature", port_request(revision=4, wall="left"))

    assert failed["ok"] is False
    assert failed["error"]["code"] == "PORT_BLOCKED_BY_LID"
    alternative = next(
        item for item in failed["error"]["alternatives"] if "envelope" in item["set"]
    )

    repaired = await _call(
        service,
        "feature",
        port_request(
            revision=4,
            request_id="safe-port-envelope",
            wall="left",
            envelope=alternative["set"]["envelope"],
        ),
    )
    assert repaired["ok"] is True
    await _assert_mounting_holes_remain_covered(service)


@pytest.mark.anyio
async def test_lid_depth_conflict_limit_is_safe_when_applied(tmp_path):
    service = DocumentService(tmp_path / "lid-limit.coalesce.json")
    await _setup(service)

    failed = await _call(
        service,
        "feature",
        lid_request(lip={"depth": quantity(5), "width": quantity(1)}),
    )

    assert failed["ok"] is False
    assert failed["error"]["code"] == "INVALID_PARAMETER"
    limits = failed["error"]["limits"]
    assert limits["parameter"] == "lip.depth"
    assert limits["current"] == 5
    assert limits["maximum"] == 4

    repaired = await _call(
        service,
        "feature",
        lid_request(
            request_id="safe-lid",
            lip={"depth": quantity(limits["suggested"]), "width": quantity(1)},
        ),
    )
    assert repaired["ok"] is True
    await _assert_mounting_holes_remain_covered(service)
