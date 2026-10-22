"""Consume durable index jobs without an HTTP server; credentials come from environment."""
import argparse
import asyncio
from dataclasses import replace
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.core.config import Settings
from app.main import create_app

async def main():
    settings=replace(Settings.from_env(),index_worker_enabled=False,ingestion_worker_enabled=False)
    app=create_app(settings)
    async with app.router.lifespan_context(app):
        print('Index worker running. Only queued jobs matching the configured model/collection are processed.',flush=True)
        await app.state.index_worker.run(asyncio.Event())

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    try:asyncio.run(main())
    except KeyboardInterrupt:pass
