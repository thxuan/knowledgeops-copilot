"""Small paid API smoke check; no PostgreSQL or source-document upload needed."""
import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.core.config import Settings
from app.core.embedding import make_embedding_client, validate_result
from app.generation.context import build_context
from app.generation.models import parse_generation
from app.generation.provider import make_generation_client


async def verify():
    settings = Settings.from_env()
    if settings.embedding.provider != 'compatible' or settings.generation.provider != 'compatible':
        raise ValueError('Both providers must be compatible')
    report = {'scope': 'API smoke check, not RAG quality evaluation', 'checks': {}}
    stage = 'embedding'
    try:
        started = time.perf_counter()
        result = await make_embedding_client(settings.embedding).embed([
            'TL-403: device compliance refresh can take up to 10 minutes.',
            'MAIL-552: message size limit is 25 MB.',
        ])
        validate_result(result, settings.embedding, 2)
        report['checks']['embedding'] = {
            'passed': True, 'model': result.model_id, 'dimensions': settings.embedding.dimensions,
            'vector_count': len(result.vectors), 'total_tokens': result.total_tokens,
            'elapsed_seconds': round(time.perf_counter() - started, 3),
        }
        stage = 'generation'
        context = build_context('TL-403: how long can compliance refresh take?', [], {}, [{
            'chunk_id': 'smoke-evidence-1', 'source': 'synthetic-smoke-fixture', 'version': 'v1',
            'text': 'TL-403: device compliance refresh can take up to 10 minutes.',
        }], settings.generation)
        started = time.perf_counter()
        raw = await make_generation_client(settings.generation).generate(context.messages, settings.generation)
        answer = parse_generation(raw)
        if answer.insufficient_context or answer.citations != ['smoke-evidence-1']:
            raise ValueError('Expected supplied synthetic evidence citation')
        report['checks']['generation'] = {
            'passed': True, 'requested_model': settings.generation.model_id,
            'schema_valid': True, 'citation_membership_valid': True,
            'elapsed_seconds': round(time.perf_counter() - started, 3),
            'answer': answer.model_dump(), 'semantic_support': 'Requires human review',
        }
    except Exception as exc:
        # Do not print credentials, request headers, URLs or raw exception messages.
        failure = {'passed': False, 'error_type': type(exc).__name__, 'code': getattr(exc, 'code', None)}
        cause = exc
        while cause is not None:
            response = getattr(cause, 'response', None)
            if response is not None:
                failure['http_status'] = response.status_code
                break
            cause = cause.__cause__
        report['checks'][stage] = failure
        report['passed'] = False
    else:
        report['passed'] = True
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, default=ROOT / 'docs/experiments/real-model-connectivity.json')
    args = parser.parse_args()
    try:
        report = asyncio.run(verify())
    except ValueError:
        print('Configuration invalid: check providers, models, keys and limits in this PowerShell session.')
        return 1
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print('Report: ' + str(args.report.resolve()))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
