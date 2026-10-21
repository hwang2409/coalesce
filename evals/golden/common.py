from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).parents[2]
FIXTURES = ROOT / "evals" / "fixtures"

def q(value): return {"value": value, "unit": "mm"}

def build_calls(source):
    calls = [("import", {"action":"create","expectedRevision":0,"requestId":"golden-import","assetId":"board","format":"kicad_pcb","path":str(source.resolve())})]
    def feature(rev, fid, kind, parameters, rid):
        return ("feature", {"action":"create","expectedRevision":rev,"requestId":rid,"featureId":fid,"kind":kind,"parameters":parameters})
    calls += [feature(1,"base","pcb_base",{"board":{"featureId":"board"},"wallThickness":q(2),"floorThickness":q(2),"clearance":q(1),"wallHeightAboveBoard":q(4),"boardSeatHeight":q(1)},"golden-base")]
    calls += [feature(2,"mounts","standoffs",{"target":{"featureId":"base"},"board":{"featureId":"board"},"selection":{"mode":"all_mounting"},"bossDiameter":q(5),"pilotDiameter":q(1)},"golden-mounts")]
    calls += [feature(3,"lid","pcb_lid",{"base":{"featureId":"base"},"thickness":q(2),"fitGap":q(.25)},"golden-lid")]
    calls += [feature(4,"base_round","fillet",{"target":{"featureId":"base"},"edges":[{"feature":"base","role":"outer_vertical","cardinality":"exactly","count":4}],"radius":q(3)},"golden-fillet")]
    calls += [feature(5,"port","port_cutout",{"base":{"featureId":"base"},"footprint":"J1","wall":"left","clearance":q(.5),"envelope":{"source":"courtyard","height":q(1),"bottomOffset":q(0)}},"golden-port")]
    calls += [feature(6,"lid_chamfer","chamfer",{"target":{"featureId":"lid"},"edges":[{"feature":"lid","role":"between(edge,top)","cardinality":"exactly","count":4}],"distance":q(1)},"golden-chamfer")]
    return calls

async def run(document, calls):
    server = StdioServerParameters(command="coalesce", args=["--document",str(document)], env={**os.environ,"PYTHONUNBUFFERED":"1"})
    async with stdio_client(server) as (read, write), ClientSession(read, write) as session:
        await session.initialize(); results=[]
        for name, args in calls:
            result=await session.call_tool(name,args); data=result.structuredContent; results.append(data)
            if not data.get("ok",False): raise RuntimeError(f"{name} failed: {data}")
        return results

def finish_artifact(out, task, calls):
    out.mkdir(parents=True,exist_ok=True); (out/"steps").mkdir(exist_ok=True); (out/"fixtures").mkdir(exist_ok=True)
    for name in ("features.kicad_pcb","features-moved.kicad_pcb"): shutil.copyfile(FIXTURES/name,out/"fixtures"/name)
    document=out/"model.coalesce.json"; results=asyncio.run(run(document,calls)); revision=results[-1]["revision"]
    valid=asyncio.run(run(document,[("check",{"action":"evaluate","revision":revision,"requirements":[{"id":"valid","kind":"valid_brep"}]})]))
    if valid[0]["results"][0]["status"]!="PASS": raise RuntimeError(valid)
    measured=asyncio.run(run(document,[("query",{"select":{"kind":"measurements"}})]))[0]
    for index, part in enumerate(measured["parts"]):
        asyncio.run(run(document,[("export",{"expectedRevision":revision,"partIds":[part["partId"]],"path":str((out/"steps"/f"part-{index}.step").resolve())})]))
    from evals.checks import check_task
    task=json.loads((ROOT/"evals"/"tasks"/f"{task}.json").read_text()); checks=check_task(task,document,out/"steps")
    (out/"golden-results.json").write_text(json.dumps(checks,indent=2,default=str))
    failed=[x for x in checks if x["status"]!="pass"]
    if failed: raise RuntimeError(f"golden checks failed: {failed}")
    return checks
