from __future__ import annotations

import asyncio
import json
import shutil
import sys
from pathlib import Path

from evals.checks import check_task, identity_snapshot

from .common import FIXTURES, build_calls, run

if __name__ == '__main__':
    out=Path(sys.argv[1]); out.mkdir(parents=True,exist_ok=True); (out/'steps').mkdir(exist_ok=True); (out/'fixtures').mkdir(exist_ok=True)
    for name in ('features.kicad_pcb','features-moved.kicad_pcb'): shutil.copyfile(FIXTURES/name,out/'fixtures'/name)
    doc=out/'model.coalesce.json'; asyncio.run(run(doc,build_calls(FIXTURES/'features.kicad_pcb')))
    inspected=asyncio.run(run(doc,[('query',{'select':{'kind':'enclosure','base':'base'}})]))[0]
    facts={'revision':7,**identity_snapshot(inspected,'port')}; (out/'before-state.json').write_text(json.dumps(facts,indent=2))
    refresh=('import',{'action':'refresh','expectedRevision':7,'requestId':'golden-refresh','assetId':'board','format':'kicad_pcb','path':str((FIXTURES/'features-moved.kicad_pcb').resolve())})
    results=asyncio.run(run(doc,[refresh])); revision=results[-1]['revision']
    asyncio.run(run(doc,[('check',{'action':'evaluate','revision':revision,'requirements':[{'id':'valid','kind':'valid_brep'}]})]))
    measured=asyncio.run(run(doc,[('query',{'select':{'kind':'measurements'}})]))[0]
    for i,part in enumerate(measured['parts']): asyncio.run(run(doc,[('export',{'expectedRevision':revision,'partIds':[part['partId']],'path':str((out/'steps'/f'part-{i}.step').resolve())})]))
    task=json.loads((Path(__file__).parents[1]/'tasks/pcb-refresh-treatments.json').read_text()); checks=check_task(task,doc,out/'steps',out/'before-state.json')
    (out/'golden-results.json').write_text(json.dumps(checks,indent=2,default=str))
    failed=[x for x in checks if x['status']!='pass']
    if failed: raise SystemExit(f'golden checks failed: {failed}')
