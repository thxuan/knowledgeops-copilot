"""Day 9 paired dev comparison. Gold is used only after retrieval."""
import argparse
import asyncio
from dataclasses import replace
from datetime import datetime, timezone, timedelta
import json
import logging
from pathlib import Path
import random
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.core.config import Settings
from app.core.embedding import make_embedding_client
from app.core.identity import LOCAL_IDENTITIES
from app.db.chunks import ChunkRepository
from app.db.ingestion import IngestionRepository
from app.db.snapshots import evaluation_snapshot, database_label
from app.db.vectors import VectorRepository
from app.evals.retrieval import measure
from app.retrieval.pipeline import RetrievalService, STRATEGIES
from app.retrieval.service import VectorService
from scripts.compare_chunks import load_corpus
from scripts.evaluate_rag import fingerprint, filter_checks, read_jsonl, validate_fixture, write_json, write_jsonl


def percentile(values, p):
    if not values:
        return None
    values = sorted(values)
    position = (len(values) - 1) * p
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    return values[lower] + (values[upper] - values[lower]) * (position - lower)


def known_sum(values):
    return sum(values) if all(v is not None for v in values) else None


def summarize_modes(rows):
    result = {}
    for strategy in STRATEGIES:
        group = [r for r in rows if r['strategy'] == strategy]
        answerable = [r for r in group if r['answerable']]
        cross = [r for r in answerable if r['question_type'] == 'cross_document']
        mean = lambda subset, key: sum(r['metrics'][key] for r in subset) / len(subset) if subset else None
        durations = [r['result']['duration_ms'] for r in group if r['status'] == 'ok']
        rerank = [r['result']['diagnostics']['rerank'] for r in group if r['status'] == 'ok']
        result[strategy] = {
            'samples': len(group), 'errors': sum(r['status'] != 'ok' for r in group),
            'mean_source_recall_at_k': mean(answerable, 'source_recall_at_k'),
            'mean_reciprocal_rank': mean(answerable, 'reciprocal_rank'),
            'mean_section_char_coverage_at_k': mean(answerable, 'section_char_coverage_at_k'),
            'cross_document_all_sources_rate': mean(cross, 'all_required_sources_recalled'),
            'cross_document_samples': len(cross),
            'p50_composed_retrieval_ms': percentile(durations, .5),
            'p95_composed_retrieval_ms': percentile(durations, .95),
            'rerank_failures': sum(r['status'] == 'failed_fallback' for r in rerank),
            'rerank_total_tokens': known_sum([r['total_tokens'] for r in rerank]) if len(rerank) == len(group) else None,
            'rerank_estimated_cost': known_sum([r['estimated_cost'] for r in rerank]) if len(rerank) == len(group) else None,
            'embedding_estimated_cost_if_run_alone': known_sum([r['embedding_cost'] for r in group]),
            'semantic_correctness': None,
        }
    return result


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--database-url', default=None)
    parser.add_argument('--candidate-k', type=int, default=20)
    parser.add_argument('--top-k', type=int, default=20)
    parser.add_argument('--repeats', type=int, default=1)
    parser.add_argument('--currency', default='unspecified')
    args = parser.parse_args()
    if not 1 <= args.top_k <= args.candidate_k <= 50 or not 1 <= args.repeats <= 5:
        parser.error('Need 1 <= top-k <= candidate-k <= 50 and repeats in 1..5')
    settings = Settings.from_env()
    if not settings.vector_database_url or settings.embedding.provider != 'compatible' or settings.retrieval.rerank.provider != 'compatible':
        parser.error('Configure database, compatible Embedding and compatible Reranker')
    if args.currency == 'unspecified' and any(p is not None for p in (
            settings.embedding.cost_per_million_tokens, settings.retrieval.rerank.cost_per_million_tokens)):
        parser.error('Specify --currency when recording prices')
    retrieval_settings = replace(settings.retrieval, candidate_k=args.candidate_k)
    run_id = datetime.now(timezone(timedelta(hours=8))).strftime('%Y%m%d-%H%M%S') + '-' + uuid4().hex[:8]
    out = ROOT / 'docs/experiments/day9' / run_id
    out.mkdir(parents=True, exist_ok=False)
    manifest = {'run_id': run_id, 'split': 'dev', 'status': 'running', 'source_db': database_label(args.database_url or settings.database_url),
        'config': {'embedding': settings.embedding.signature, 'collection': settings.embedding.collection,
            'retrieval': retrieval_settings.public_config, 'top_k': args.top_k, 'repeats': args.repeats,
            'currency': args.currency, 'embedding_price': settings.embedding.cost_per_million_tokens,
            'code_and_data_sha256': fingerprint()},
        'scope': 'Retrieval only; source coverage does not measure generated answer quality.',
        'latency_note': 'Paired candidates are shared. Composed times = shared vector candidate-K search + '
            'lexical/fusion for hybrid + per-mode finalization. Not three independent end-to-end measurements; '
            'vector baseline also retrieves candidate-K, not production top-K.',
        'cost_note': 'Query embedding is actually paid once per question/repeat, reused for all modes. '
            'Per-mode embedding cost is a standalone allocation; do not sum it across modes. '
            'Unknown or failed-call billing is null. No generation calls.',
        'held_out_test': 'Not evaluated. Freeze chosen configuration with evaluate_rag dev before test.'}
    write_json(out / 'manifest.json', manifest)
    intake, schema = evaluation_snapshot(args.database_url or settings.database_url, out)
    manifest["snapshot_schema"] = schema
    intake.initialize()
    chunks = ChunkRepository(intake)
    chunks.initialize()
    vectors = VectorRepository(args.database_url or settings.database_url, settings.embedding)
    vectors.initialize()
    corpus = load_corpus()
    try:
        source_ids, docs = validate_fixture(intake, chunks, vectors, corpus)
    except ValueError:
        manifest.update(status='failed', error_code='FIXTURE_MISMATCH')
        write_json(out / 'manifest.json', manifest)
        raise
    manifest['corpus'] = [{k: d[k] for k in ('document_id', 'source', 'version', 'chunk_set_id')} for d in docs]
    write_json(out / 'manifest.json', manifest)
    service = RetrievalService(VectorService(chunks, vectors, make_embedding_client(settings.embedding),
                                             settings.embedding), retrieval_settings)
    questions = [q for q in read_jsonl(ROOT / 'eval_data/questions.jsonl') if q['split'] == 'dev']
    anchors = read_jsonl(ROOT / 'eval_data/day4-source-spans.jsonl')
    rows, calls = [], []
    randomizer = random.Random(9)
    for repeat in range(1, args.repeats + 1):
        for q in questions:
            identity = LOCAL_IDENTITIES[q['allowed_user_ids'][0]]
            prepared, error = None, None
            try:
                prepared = await service.prepare(q['question'], identity, args.candidate_k)
                events = await asyncio.to_thread(vectors.batch_events, prepared['result']['run_id'])
            except Exception as exc:
                error = getattr(exc, 'code', type(exc).__name__)
                events = []
            embedding_cost = known_sum([e['estimated_cost'] for e in events]) if prepared else None
            calls.append({'question_id': q['question_id'], 'repeat': repeat, 'embedding_batches': events,
                          'estimated_cost': embedding_cost, 'error_code': error})
            modes = list(STRATEGIES)
            randomizer.shuffle(modes)
            for strategy in modes:
                row = {'question_id': q['question_id'], 'question_type': q['question_type'],
                    'question': q['question'], 'answerable': q['answerable'], 'user_id': identity.user_id,
                    'repeat': repeat, 'strategy': strategy, 'status': 'error', 'embedding_cost': embedding_cost}
                hits = []
                try:
                    if prepared is None:
                        raise ValueError('Shared retrieval failed')
                    result = await service.finish(prepared, identity, args.top_k, strategy)
                    hits = result['hits']
                    row.update(status='ok', result=result,
                        filter_checks=filter_checks(hits, identity, corpus, source_ids),
                        candidate_filter_checks=filter_checks(result['diagnostics']['candidates'], identity, corpus, source_ids),
                        candidate_metrics=measure(q, result['diagnostics']['candidates'], anchors, source_ids))
                except Exception as exc:
                    row['error_code'] = error or getattr(exc, 'code', type(exc).__name__)
                row['metrics'] = measure(q, hits, anchors, source_ids)
                rows.append(row)
                write_jsonl(out / 'results.jsonl', rows)
                write_jsonl(out / 'shared-embedding-calls.jsonl', calls)
    summary = summarize_modes(rows)
    write_json(out / 'summary.json', summary)
    manifest.update(status='completed', sample_count=len(rows),
        actually_incurred_embedding_cost=known_sum([c['estimated_cost'] for c in calls]))
    write_json(out / 'manifest.json', manifest)
    lines = ['# Day 9 开发集检索对照', '',
        f"Embedding：{settings.embedding.provider}/{settings.embedding.model_id}；Rerank：{settings.retrieval.rerank.public_config['model_id']}。",
        f'每路候选 K={args.candidate_k}，融合后截至 {args.candidate_k}，最终 K={args.top_k}；重复 {args.repeats} 次。', '',
        '| 模式 | 来源 Recall | MRR | 跨文档全来源 | P50/P95 组合耗时 ms | Rerank Token | Rerank 费用 | 回退/错误 |',
        '| --- | --- | --- | --- | --- | --- | --- | --- |']
    for mode, s in summary.items():
        lines.append(f"| {mode} | {s['mean_source_recall_at_k']} | {s['mean_reciprocal_rank']} | "
            f"{s['cross_document_all_sources_rate']} | {s['p50_composed_retrieval_ms']}/{s['p95_composed_retrieval_ms']} | "
            f"{s['rerank_total_tokens']} | {s['rerank_estimated_cost']} | {s['rerank_failures']}/{s['errors']} |")
    lines += ['', '这是检索实验记录，不是答案正确性评测。',
        '费用 null 表示未提供价格/usage或调用失败。组合耗时共享候选召回，不能当作线上独立请求时延。',
        '来源命中只要求冻结章节坐标相交，不等于每个答案要点齐全；未生成答案或评分语义正确性。',
        '保留测试未运行；真实模型开发集对照和默认方案冻结尚需另行完成。']
    (out / 'comparison.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print(json.dumps({'run_dir': str(out.resolve()), 'summary': summary}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    logging.getLogger('knowledgeops').setLevel(logging.WARNING)
    asyncio.run(main())
