"""Real tool-loop smoke; no automatic ticket approval or fake success fallback."""
import argparse
import asyncio
import json
from pathlib import Path
import sys
import time
from uuid import uuid4
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.agent.provider import CompatibleAgentModel
from app.agent.service import AgentService
from app.core.config import Settings
from app.core.identity import LOCAL_IDENTITIES
from app.db.agent import AgentRepository
from app.main import create_app

async def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--message',default='请调用计算器计算 17 乘以 23，然后用结果加 9，告诉我最终数值。')
    parser.add_argument('--output',type=Path,default=Path(__file__).resolve().parents[1]/'docs/experiments/day10-12'/('real-'+time.strftime('%Y%m%d-%H%M%S')+'.json'))
    args=parser.parse_args()
    settings=Settings.from_env()
    if settings.agent.provider!='compatible':parser.error('Configure the compatible Agent provider first')
    app=create_app(settings)
    async with app.router.lifespan_context(app):
        service=app.state.persistent_agent
        if service is None:parser.error('Agent PostgreSQL is not configured')
        identity=LOCAL_IDENTITIES['u_engineer_001']
        state=await asyncio.to_thread(service.repo.create,identity,args.message,uuid4().hex,'real-agent-smoke')
        await service.execute(state['run_id'],identity)
        state=await asyncio.to_thread(service.repo.get,state['run_id'],identity)
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(state,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({'run_id':state['run_id'],'status':state['status'],'outcome':state['outcome'],
            'steps':state['steps'],'tools':[t['name'] for t in state['trace']],'report':str(args.output)},ensure_ascii=False))
        if state['status']=='failed':raise SystemExit(1)
if __name__=='__main__':asyncio.run(main())
