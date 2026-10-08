"""Run 10x3 local scenario oracles plus bounded concurrent API workload."""
import argparse
import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import statistics
import sys
import time
from uuid import uuid4
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from concurrent.futures import ThreadPoolExecutor
from app.agent.contracts import AgentSettings
from app.core.config import Settings
from app.core.identity import Identity, LOCAL_IDENTITIES
from app.db.agent import AgentRepository
from app.evals.agent import run_scenario
from app.main import create_app

ROOT=Path(__file__).resolve().parents[1]
def percentile(values,p):
    values=sorted(values)
    return values[min(len(values)-1,max(0,int((len(values)-1)*p)))]

async def evaluate(repo,identity,scenarios):
    results=[]
    for scenario in scenarios:
        for attempt in range(1,4):
            result=await run_scenario(repo,identity,scenario)
            results.append({**result,'attempt':attempt})
    return results

def benchmark(settings,output,identity):
    LOCAL_IDENTITIES[identity.user_id]=identity
    app=create_app(Settings(app_env='test',ingestion_worker_enabled=False,database_url=settings.database_url,agent=settings))
    headers={'X-Local-User-Id':identity.user_id}
    with TestClient(app) as client:
        def request(index):
            start=time.perf_counter()
            message='计算 12 + 28'
            response=client.post('/agent',headers=headers,json={'message':message,'request_key':uuid4().hex})
            if response.status_code!=202:
                return {'status':response.status_code,'success':False,'latency_ms':(time.perf_counter()-start)*1000}
            rid=response.json()['run_id']
            first_text=None
            for _ in range(400):
                state=client.get('/agent/'+rid,headers=headers).json()
                if first_text is None and any(e['type']=='text_delta' for e in state['events']):
                    first_text=(time.perf_counter()-start)*1000
                if state['status']!='running':break
                # Same run ID, explicit resume if an admission slot was unavailable.
                if state['phase']=='queued':client.post('/agent/'+rid+'/resume',headers=headers)
                time.sleep(.02)
            return {'run_id':rid,'success':state['status']=='completed' and '40' in (state['answer'] or ''),
                    'status':state['status'],'input_chars':len(message),'latency_ms':(time.perf_counter()-start)*1000,
                    'first_text_event_ms':first_text,'ttft_ms':None,'estimated_cost':0.0}
        with ThreadPoolExecutor(max_workers=4) as pool:
            rows=list(pool.map(request,range(12)))
    latencies=[r['latency_ms'] for r in rows]
    return {'requests':12,'concurrency':4,'successful':sum(r['success'] for r in rows),
            'failure_rate':sum(not r['success'] for r in rows)/len(rows),
            'p50_ms':statistics.median(latencies),'p95_ms':percentile(latencies,.95),
            'ttft_ms':None,'ttft_note':'No upstream token stream; first_text_event_ms measures validated buffered answer delivery, not model TTFT.',
            'estimated_cost':0.0,'scope':'Local Fake model + real PostgreSQL + FastAPI; 12 samples, unstable tail estimate.', 'rows':rows}

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,default=ROOT/'docs/experiments/day10-12'/time.strftime('%Y%m%d-%H%M%S'))
    args=parser.parse_args()
    settings=replace(AgentSettings.from_env(),provider='fake',model_id='fake-agent-v1',requests_per_minute=200)
    settings.validate()
    if not settings.database_url or not settings.business_database_url:parser.error('Set Agent database and SELECT-only business URLs')
    args.output.mkdir(parents=True,exist_ok=True)
    repo=AgentRepository(settings)
    repo.initialize()
    identity=Identity('agent_eval_'+uuid4().hex,'agent_eval_'+uuid4().hex,frozenset({'engineering-cn'}))
    repo.seed_demo(identity)
    scenarios=[json.loads(line) for line in (ROOT/'eval_data/agent_tasks.jsonl').read_text(encoding='utf-8-sig').splitlines()]
    results=asyncio.run(evaluate(repo,identity,scenarios))
    (args.output/'agent-results.json').write_text(json.dumps({'mode':'scripted_policy_real_postgresql',
        'passed':sum(r['passed'] for r in results),'total':len(results),'results':results,
        'limits':'Policy fixtures prove executor oracles, not model task competence. Restart scenarios reconstruct repositories; subprocess crash coverage lives in integration tests.'},ensure_ascii=False,indent=2),encoding='utf-8')
    bench=benchmark(settings,args.output,identity)
    (args.output/'concurrency.json').write_text(json.dumps(bench,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'output':str(args.output),'passed':sum(r['passed'] for r in results),'total':len(results),
        'benchmark_success':bench['successful'],'p50_ms':bench['p50_ms'],'p95_ms':bench['p95_ms']}))
    if not all(r['passed'] for r in results) or bench['successful']!=12:raise SystemExit(1)
if __name__=='__main__':main()
