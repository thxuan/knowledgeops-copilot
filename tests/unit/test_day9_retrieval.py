from tests.doubles import LexicalRerankDouble
"""Day 9 rank fusion, provider contracts and failure boundaries."""
import asyncio
from dataclasses import replace
import json
import math
from types import SimpleNamespace
import httpx
import pytest
from app.core.config import Settings
from app.core.errors import AppError
from app.core.identity import LOCAL_IDENTITIES
from app.retrieval.lexical import bm25, rrf, tokenize
from app.retrieval.pipeline import RetrievalService, RetrievalSettings
from app.retrieval.rerank import (CompatibleRerankClient, RerankError, RerankResult,
                                  RerankSettings, validate_result)


def hit(key, text='', score=0, doc='doc'):
    return {'chunk_id': key, 'document_id': doc, 'chunk_set_id': 'set', 'text': text,
            'title': '', 'heading_paths': [], 'score': score, 'source': 'source', 'version': 'v1',
            'text_hash': 'hash', 'parser_version': 'parser', 'start_char': 0, 'end_char': len(text),
            'start_line': 1, 'end_line': 1}


def test_fixed_tokenizer_preserves_entities_and_chinese():
    assert tokenize('TL-403 Windows 11 23H2 72小时 不允许') == [
        'tl-403', 'windows', '11', '23h2', '72', '小时', '不允', '允许']
    assert tokenize('甲') == ['甲']
    assert tokenize('25.5 MB') == ['25.5', 'mb']


def test_bm25_identifier_match_empty_and_deterministic_ties():
    docs = [hit('z', 'TL-403 设备修复'), hit('a', 'TL-403 设备修复'), hit('other', 'TL-404 网络问题')]
    assert [h['chunk_id'] for h in bm25('TL-403', docs, 5)] == ['a', 'z']
    assert bm25('unknown', docs, 5) == []
    assert bm25('TL-403', [], 5) == []
    assert docs[0]['score'] == 0


def test_bm25_numeric_formula():
    result = bm25('vpn', [hit('a', 'vpn vpn'), hit('b', 'mail')], 2)
    expected = math.log1p(1.5 / 1.5) * 2 * 2.2 / (2 + 1.2 * (.25 + .75 * 2 / 1.5))
    assert result[0]['score'] == pytest.approx(expected)


def test_rrf_discards_score_scales_and_deduplicates():
    a, b = hit('a', score=.9), hit('b', score=.1)
    keyword = [hit('b', score=1e6), hit('a', score=1e-3)]
    fused = rrf({'vector': [a, b], 'keyword': keyword})
    assert fused[0]['score'] == pytest.approx(1/61 + 1/62)
    assert fused[0]['score'] == fused[1]['score']
    assert fused[0]['chunk_id'] == 'a'
    assert fused[0]['ranks'] == {'vector': 1, 'keyword': 2}
    assert rrf({'vector': [a, a]})[0]['score'] == pytest.approx(1/61)
    assert a['score'] == .9


def test_compatible_rerank_maps_indices_and_usage_without_secrets():
    settings = RerankSettings(provider='compatible', endpoint='https://fixture.invalid/v1/reranks', api_key='secret')
    def handler(request):
        payload = json.loads(request.content)
        assert payload == {'model': 'qwen3-rerank', 'query': 'vpn', 'documents': ['bad', 'good'], 'top_n': 2}
        assert request.headers['Authorization'] == 'Bearer secret'
        return httpx.Response(200, json={'model': 'qwen3-rerank', 'id': 'request',
            'results': [{'index': 1, 'relevance_score': .9}, {'index': 0, 'relevance_score': .1}],
            'usage': {'total_tokens': 20}})
    result = asyncio.run(CompatibleRerankClient(settings, httpx.MockTransport(handler)).rerank('vpn', ['bad', 'good']))
    assert result.results == [(1, .9), (0, .1)] and result.total_tokens == 20
    assert 'secret' not in json.dumps(settings.public_config)


@pytest.mark.parametrize('results', [[(0, .5)], [(0, .5), (0, .4)], [(0, .5), (2, .4)],
    [(True, .5), (1, .4)], [(0, float('nan')), (1, .4)], [(0, True), (1, .4)], [(0, 1.1), (1, .4)]])
def test_provider_bad_count_indices_and_scores_are_rejected(results):
    with pytest.raises(RerankError):
        validate_result(RerankResult(results), 2, True)


@pytest.mark.parametrize('status', [429, 500])
def test_provider_errors_are_sanitized(status):
    settings = RerankSettings(provider='compatible', endpoint='https://fixture.invalid/v1/reranks', api_key='secret')
    with pytest.raises(RerankError) as error:
        asyncio.run(CompatibleRerankClient(settings, httpx.MockTransport(
            lambda _: httpx.Response(status, text='secret provider body'))).rerank('vpn', ['text']))
    assert 'secret' not in str(error.value)


class VectorDouble:
    def __init__(self):
        self.allowed = [{'document_id': 'doc', 'chunk_set_id': 'set'}]
        self.calls = []
        self.vectors = self
    def allowed_sets(self, identity, filters=None):
        return self.allowed
    def lexical_candidates(self, allowed, tenant_id):
        return [hit('good', 'TL-403 repaired'), hit('bad', 'unrelated')]
    async def search(self, query, identity, top_k, *args):
        self.calls.append((query, top_k))
        return {'collection': 'test', 'model_id': 'fake', 'dimensions': 2, 'mode': 'exact',
                'run_id': 'run', 'duration_ms': 1,
                'hits': [hit('bad', 'unrelated', .9), hit('good', 'TL-403 repaired', .1)][:top_k]}


IDENTITY = LOCAL_IDENTITIES['u_engineer_001']


def test_three_modes_share_candidates_keep_trace_and_leave_query_unchanged():
    async def run():
        vectors = VectorDouble()
        service = RetrievalService(vectors, RetrievalSettings(candidate_k=2, rerank=RerankSettings(provider='compatible', api_key='test-only', endpoint='https://rerank.test/v1/reranks', cost_per_million_tokens=0)), reranker=LexicalRerankDouble())
        query = 'TL-403 repaired'
        prepared = await service.prepare(query, IDENTITY, 2)
        results = {m: await service.finish(prepared, IDENTITY, 1, m) for m in ('vector', 'hybrid', 'hybrid_rerank')}
        assert vectors.calls == [(query, 2)]
        assert results['vector']['hits'][0]['chunk_id'] == 'bad'
        assert results['hybrid']['hits'][0]['chunk_id'] == 'good'
        assert results['hybrid_rerank']['hits'][0]['chunk_id'] == 'good'
        assert results['hybrid']['score_type'] == 'rrf'
        assert results['hybrid_rerank']['score_type'] == 'rerank_relevance'
        d = results['hybrid_rerank']['diagnostics']
        assert d['candidate_count'] == 2 and d['final_count'] == 1
        assert d['original_query'] == query and not d['query_rewritten']
        assert d['rerank']['estimated_cost'] == 0
    asyncio.run(run())


@pytest.mark.parametrize('failure', ['timeout', 'malformed'])
def test_rerank_failure_falls_back_with_unknown_cost(failure):
    async def run():
        class Broken:
            async def rerank(self, query, documents):
                if failure == 'timeout':
                    await asyncio.sleep(.1)
                return RerankResult([(99, .9)])
        service = RetrievalService(VectorDouble(), RetrievalSettings(candidate_k=2,
            rerank=RerankSettings(provider='compatible', api_key='test-only', endpoint='https://rerank.test/v1/reranks', cost_per_million_tokens=0, timeout_seconds=.001)), Broken())
        result = await service.retrieve('TL-403', IDENTITY, 1, strategy='hybrid_rerank')
        assert result['score_type'] == 'rrf'
        assert result['diagnostics']['rerank']['status'] == 'failed_fallback'
        assert result['diagnostics']['rerank']['estimated_cost'] is None
        assert result['hits'][0]['chunk_id'] == 'good'
    asyncio.run(run())


def test_permission_revoked_during_rerank_drops_returned_evidence():
    async def run():
        vectors = VectorDouble()
        class Revoke:
            async def rerank(self, query, documents):
                vectors.allowed = []
                return RerankResult([(i, 1.) for i in range(len(documents))])
        service = RetrievalService(vectors, RetrievalSettings(candidate_k=2,
            rerank=RerankSettings(provider='compatible', api_key='test-only', endpoint='https://rerank.test/v1/reranks', cost_per_million_tokens=0)), Revoke())
        result = await service.retrieve('TL-403', IDENTITY, 1, strategy='hybrid_rerank')
        assert not result['hits'] and not result['diagnostics']['candidates']
        assert 'TL-403 repaired' not in json.dumps(result)
    asyncio.run(run())


def test_empty_authorized_candidates_skip_rerank():
    async def run():
        vectors = VectorDouble()
        vectors.allowed = []
        class NeverCall:
            async def rerank(self, *args):
                raise AssertionError('Empty candidates must not call a model')
        service = RetrievalService(vectors, RetrievalSettings(candidate_k=2,
            rerank=RerankSettings(provider='compatible', api_key='test-only', endpoint='https://rerank.test/v1/reranks', cost_per_million_tokens=0)), NeverCall())
        result = await service.retrieve('TL-403', IDENTITY, 1, strategy='hybrid_rerank')
        assert result['hits'] == [] and result['diagnostics']['rerank']['status'] == 'empty'
    asyncio.run(run())


def test_missing_reranker_and_invalid_candidate_configuration():
    service = RetrievalService(VectorDouble())
    with pytest.raises(AppError) as error:
        asyncio.run(service.retrieve('x', IDENTITY, strategy='hybrid_rerank'))
    assert error.value.code == 'RERANK_NOT_CONFIGURED'
    with pytest.raises(ValueError):
        asyncio.run(service.retrieve('x', IDENTITY, top_k=5, candidate_k=2))
    with pytest.raises(ValueError):
        replace(Settings(), retrieval=RetrievalSettings(strategy='hybrid_rerank')).validate()


def test_config_reads_env_without_exposing_key(monkeypatch):
    monkeypatch.setenv('KNOWLEDGEOPS_RETRIEVAL_STRATEGY', 'hybrid_rerank')
    monkeypatch.setenv('KNOWLEDGEOPS_RERANK_PROVIDER', 'compatible')
    monkeypatch.setenv('KNOWLEDGEOPS_RERANK_ENDPOINT', 'https://fixture.invalid/v1/reranks')
    monkeypatch.setenv('KNOWLEDGEOPS_RERANK_API_KEY', 'secret')
    settings = Settings.from_env()
    assert settings.retrieval.rerank.api_key == 'secret'
    assert 'secret' not in json.dumps(settings.retrieval.public_config)


def test_comparison_summary_counts_errors_and_unknown_billing():
    from scripts.compare_retrieval import summarize_modes
    metrics = {'source_recall_at_k': 0., 'reciprocal_rank': 0.,
               'section_char_coverage_at_k': 0., 'all_required_sources_recalled': False}
    failed = {'strategy': 'hybrid_rerank', 'answerable': True, 'question_type': 'cross_document',
              'status': 'error', 'metrics': metrics, 'embedding_cost': None}
    good = {'strategy': 'hybrid_rerank', 'answerable': True, 'question_type': 'cross_document',
            'status': 'ok', 'metrics': {k: 1. for k in metrics}, 'embedding_cost': 0.1,
            'result': {'duration_ms': 10, 'diagnostics': {'rerank': {'status': 'failed_fallback',
                'total_tokens': None, 'estimated_cost': None}}}}
    s = summarize_modes([failed, good])['hybrid_rerank']
    assert s['errors'] == 1 and s['rerank_failures'] == 1
    assert s['mean_source_recall_at_k'] == .5
    assert s['cross_document_all_sources_rate'] == .5
    assert s['rerank_estimated_cost'] is None and s['embedding_estimated_cost_if_run_alone'] is None


def test_offline_rag_freeze_records_retrieval_settings():
    from scripts.evaluate_rag import safe_config
    args = SimpleNamespace(top_k=5, input_price=None, output_price=None, currency='unspecified')
    baseline = safe_config(Settings(), args)
    hybrid = safe_config(replace(Settings(), retrieval=RetrievalSettings(strategy='hybrid')), args)
    assert baseline['retrieval']['strategy'] == 'vector'
    assert baseline['retrieval'] != hybrid['retrieval']
    assert 'api_key' not in json.dumps(hybrid)
