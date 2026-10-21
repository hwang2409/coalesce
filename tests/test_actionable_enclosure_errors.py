import copy
import shutil

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
    assert set(alternative["set"]) <= {"wall", "envelope"}
    assert alternative["set"]

    repaired_request = port_request(
        revision=3,
        request_id="safe-port-placement",
        wall=alternative["set"].get("wall", "left"),
        envelope=alternative["set"].get(
            "envelope", explicit_envelope(width=4, height=2, bottom=-1)
        ),
    )
    repaired = await _call(service, "feature", repaired_request)
    assert repaired["ok"] is True
    await _assert_mounting_holes_remain_covered(service)


@pytest.mark.anyio
async def test_port_lid_conflict_never_shrinks_connector_envelope(tmp_path):
    service = DocumentService(tmp_path / "port-lid-alternative.coalesce.json")
    await _setup(service, lid=True)
    requested = explicit_envelope(width=2, height=2, bottom=1)

    failed = await _call(
        service,
        "feature",
        port_request(revision=4, wall="left", envelope=requested),
    )

    assert failed["ok"] is False
    assert failed["error"]["code"] == "PORT_BLOCKED_BY_LID"
    for alternative in failed["error"].get("alternatives", []):
        envelope = alternative["set"].get("envelope")
        if envelope is not None:
            assert envelope["width"] == requested["width"]
            assert envelope["height"] == requested["height"]


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


@pytest.mark.anyio
async def test_off_center_port_clearance_suggestion_succeeds_unchanged(tmp_path):
    board = tmp_path / "off-center.kicad_pcb"
    shutil.copyfile(FIXTURES / "features.kicad_pcb", board)
    board.write_text(
        board.read_text().replace(
            '(uuid "front-id") (at 1 5)', '(uuid "front-id") (at 1 2)'
        )
    )
    service = DocumentService(tmp_path / "off-center.coalesce.json")
    assert (await _call(service, "import", import_request(board)))["ok"]
    assert (await _call(service, "feature", base_request()))["ok"]
    request = port_request(
        revision=2,
        wall="left",
        envelope=explicit_envelope(width=2, height=2, bottom=1),
        clearance=quantity(5),
    )

    failed = await _call(service, "feature", request)

    assert failed["ok"] is False
    limits = failed["error"]["limits"]
    repaired = copy.deepcopy(request)
    repaired["requestId"] = "off-center-repair"
    repaired["parameters"]["clearance"] = quantity(limits["suggested"])
    assert (await _call(service, "feature", repaired))["ok"] is True


def _decimal_places(value):
    return len(str(value).partition(".")[2])


def _apply_limit(request, parameter, value):
    repaired = copy.deepcopy(request)
    if parameter == "bossDiameter":
        repaired["parameters"]["bossDiameter"] = quantity(value)
    elif parameter == "lip.depth":
        repaired["parameters"]["lip"]["depth"] = quantity(value)
    elif parameter == "clearance":
        repaired["parameters"]["clearance"] = quantity(value)
    else:
        raise AssertionError(f"unsupported test remedy: {parameter}")
    return repaired


def _apply_set(request, values):
    repaired = copy.deepcopy(request)
    repaired["parameters"].update(values)
    return repaired


async def _conflict(service, scenario, board_path=None):
    if scenario == "standoff-limit":
        await _setup(service, standoffs=False)
        request = standoffs_request(bossDiameter=quantity(7))
    elif scenario == "standoff-port":
        await _setup(service)
        request = port_request(
            revision=3,
            wall="left",
            envelope=explicit_envelope(width=4, height=2, bottom=-1),
        )
    elif scenario == "lid-port":
        await _setup(service, lid=True)
        request = port_request(
            revision=4,
            wall="left",
            envelope=explicit_envelope(width=2, height=2, bottom=1),
        )
    elif scenario == "lid-limit":
        await _setup(service)
        request = lid_request(lip={"depth": quantity(5), "width": quantity(1)})
    else:
        assert scenario == "off-center"
        assert board_path is not None
        assert (await _call(service, "import", import_request(board_path)))["ok"]
        assert (await _call(service, "feature", base_request()))["ok"]
        request = port_request(
            revision=2,
            wall="left",
            envelope=explicit_envelope(width=2, height=2, bottom=1),
            clearance=quantity(5),
        )
    failed = await _call(service, "feature", request)
    assert failed["ok"] is False
    return request, failed["error"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "scenario",
    ["standoff-limit", "standoff-port", "lid-port", "lid-limit", "off-center"],
)
async def test_every_enclosure_remedy_is_rounded_and_succeeds_through_mcp(
    tmp_path, scenario
):
    board = None
    if scenario == "off-center":
        board = tmp_path / "off-center-source.kicad_pcb"
        shutil.copyfile(FIXTURES / "features.kicad_pcb", board)
        board.write_text(
            board.read_text().replace(
                '(uuid "front-id") (at 1 5)', '(uuid "front-id") (at 1 2)'
            )
        )
    probe = DocumentService(tmp_path / "probe.coalesce.json")
    request, error = await _conflict(probe, scenario, board)
    remedies = []
    limits = error.get("limits")
    if limits:
        for name in ("minimum", "maximum", "suggested"):
            if name in limits:
                value = limits[name]
                assert _decimal_places(value) <= 2
                remedies.append(_apply_limit(request, limits["parameter"], value))
    for alternative in error.get("alternatives", []):
        for value in alternative.get("limits", {}).values():
            if isinstance(value, (int, float)):
                assert _decimal_places(value) <= 2
        remedies.append(_apply_set(request, alternative["set"]))

    for index, repaired in enumerate(remedies):
        service = DocumentService(tmp_path / f"repair-{index}.coalesce.json")
        original, _ = await _conflict(service, scenario, board)
        repaired["expectedRevision"] = original["expectedRevision"]
        repaired["requestId"] = f"repair-{index}"
        result = await _call(service, "feature", repaired)
        assert result["ok"] is True, (scenario, repaired, result)
        if scenario != "off-center":
            await _assert_mounting_holes_remain_covered(service)
