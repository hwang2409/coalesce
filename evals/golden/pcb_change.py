from __future__ import annotations

import asyncio
import json
import shutil
import sys
from pathlib import Path

from evals.checks import check_task, identity_snapshot

from .common import FIXTURES, build_calls, q, run

if __name__ == "__main__":
    out=Path(sys.argv[1]); out.mkdir(parents=True,exist_ok=True); (out/"steps").mkdir(exist_ok=True); (out/"fixtures").mkdir(exist_ok=True)
    for name in ("features.kicad_pcb","features-moved.kicad_pcb"): shutil.copyfile(FIXTURES/name,out/"fixtures"/name)
    doc=out/"model.coalesce.json"; calls=build_calls(FIXTURES/"features.kicad_pcb")[:4]
    calls += [('feature',{'action':'create','expectedRevision':4,'requestId':'golden-port','featureId':'port','kind':'port_cutout','parameters':{'base':{'featureId':'base'},'footprint':'J1','wall':'left','clearance':q(.5),'envelope':{'source':'courtyard','height':q(1),'bottomOffset':q(0)}}})]
    # keep the pre-edit document identity, then update only the wall thickness.
    before=asyncio.run(run(doc,calls+[('query',{'select':{'kind':'enclosure','base':'base'}})]))[-1]
    facts={'revision':before['revision'],'wallThickness':before['base']['minimumWallThickness']['value'],**identity_snapshot(before,'port')}
    (out/"before-state.json").write_text(json.dumps(facts,indent=2))
    calls += [('feature',{'action':'update','expectedRevision':5,'requestId':'golden-wall-update','featureId':'base','kind':'pcb_base','set':{'wallThickness':q(3)}})]
    results=asyncio.run(run(doc,calls)); revision=results[-1]['revision']
    asyncio.run(run(doc,[('check',{'action':'evaluate','revision':revision,'requirements':[{'id':'valid','kind':'valid_brep'}]})]))
    measured=asyncio.run(run(doc,[('query',{'select':{'kind':'measurements'}})]))[0]
    for i,part in enumerate(measured['parts']): asyncio.run(run(doc,[('export',{'expectedRevision':revision,'partIds':[part['partId']],'path':str((out/'steps'/f'part-{i}.step').resolve())})]))
    task=json.loads((Path(__file__).parents[1]/'tasks/pcb-change.json').read_text()); checks=check_task(task,doc,out/'steps',out/'before-state.json')
    (out/'golden-results.json').write_text(json.dumps(checks,indent=2,default=str))
    failed=[x for x in checks if x['status']!='pass']
    if failed: raise SystemExit(f'golden checks failed: {failed}')
