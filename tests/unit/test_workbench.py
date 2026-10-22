"""Controls must change actual retrieval and remain isolated between requests."""
import asyncio
from types import SimpleNamespace
import pytest
from pydantic import ValidationError
from app.api.workbench_schemas import WorkbenchChatRequest
from app.api.workbench_routes import scope_services
from app.core.config import Settings
from app.core.embedding import EmbeddingSettings
from app.core.errors import AppError
from app.core.identity import LOCAL_IDENTITIES
from app.evals.workbench import selected_question, fixture
from app.generation.provider import GenerationSettings
from app.retrieval.pipeline import RetrievalService, RetrievalSettings
from tests.unit.test_day9_retrieval import VectorDouble

IDENTITY = LOCAL_IDENTITIES['u_engineer_001']

@pytest.mark.parametrize('controls', [
    {'semantic': False, 'keyword': False},
    {'semantic': True, 'keyword': True, 'rrf': False},
    {'semantic_k': True}, {'keyword_k': 51}, {'rrf_constant': 0}])
def test_invalid_controls_are_rejected(controls):
    with pytest.raises(ValidationError):
        WorkbenchChatRequest(message='question', retrieval=controls)


def test_final_window_and_credential_injection_are_rejected():
    for body in [{'top_k': 8, 'retrieval': {'fusion_k': 5}},
                 {'generation': {'api_key': 'private'}},
                 {'generation': {'base_url': 'https://other.invalid'}},
                 {'generation': {'temperature': float('nan')}}]:
        with pytest.raises(ValidationError):
            WorkbenchChatRequest(message='q', **body)


def test_keyword_only_never_calls_embedding_or_vector_search():
    async def run():
        double = VectorDouble()
        double.settings = EmbeddingSettings()
        service = RetrievalService(double, RetrievalSettings(strategy='bm25', keyword_k=1))
        result = await service.retrieve('TL-403', IDENTITY, top_k=1)
        assert double.calls == []
        assert result['score_type'] == 'bm25'
        assert len(result['hits']) == 1
        assert not result['diagnostics']['stages']['vector']
        assert result['diagnostics']['timings_ms']['rrf'] == 0
    asyncio.run(run())


@pytest.mark.parametrize("profile", ["ranked", "coverage", "multi_query"])
def test_separate_windows_change_each_stage_without_mutating_settings(profile):
    async def run():
        double = VectorDouble()
        config = RetrievalSettings(strategy='hybrid', candidate_k=20, vector_k=2, keyword_k=1, fusion_k=1)
        service = RetrievalService(double, config)
        result = await service.retrieve('TL-403', IDENTITY, top_k=1, retrieval_profile=profile)
        assert double.calls == [('TL-403', 2)]
        stages = result['diagnostics']['stages']
        assert len(stages['vector']) == 2 and len(stages['keyword']) == 1 and len(stages['fusion']) == 1
        assert result['hits'][0]['chunk_id'] == 'good'
        assert config.vector_k == 2 and config.keyword_k == 1
    asyncio.run(run())


def test_request_scope_keeps_shared_model_and_retrieval_immutable():
    config = Settings(generation=GenerationSettings(provider='compatible', model_id='configured-model', api_key='private-test-key'))
    client = object()
    app = SimpleNamespace(state=SimpleNamespace(settings=config, chat=SimpleNamespace(settings=config.generation,client=client),
        conversations=object(),vector_service=object()))
    first = WorkbenchChatRequest(message='q',generation={'temperature': .2,'top_p': .7},retrieval={'semantic_k': 10})
    second = WorkbenchChatRequest(message='q',generation={'temperature': .9,'top_p': None},retrieval={'keyword': False,'rrf': False})
    sa, ca = scope_services(app,first)
    sb, cb = scope_services(app,second)
    assert sa.settings.temperature == .2 and sb.settings.temperature == .9
    assert sa.settings.top_p == .7 and sb.settings.top_p is None
    assert config.generation.temperature == 0 and config.retrieval.strategy == 'vector'
    assert sa.vectors.settings.strategy == 'hybrid' and sb.vectors.settings.strategy == 'vector'
    assert sa.client is client and sb.client is client
    assert 'private-test-key' not in str(ca) + str(cb)


def test_unconfigured_rerank_returns_explicit_error():
    config = Settings()
    app = SimpleNamespace(state=SimpleNamespace(settings=config, chat=SimpleNamespace(settings=config.generation,client=object())))
    with pytest.raises(AppError) as error:
        scope_services(app,WorkbenchChatRequest(message='q',retrieval={'rerank': True}))
    assert error.value.code == 'RERANK_NOT_CONFIGURED'


def test_gold_does_not_follow_changed_question_history_or_unauthorized_identity():
    q = next(q for q in fixture()[0] if q['question_id'] == 'F005')
    assert selected_question('F005',q['question'],IDENTITY,None)[0] is not None
    assert selected_question('F005',q['question']+' changed',IDENTITY,None)[0] is None
    assert selected_question('F005',q['question'],IDENTITY,'history')[0] is None
    restricted = next(q for q in fixture()[0] if 'u_employee_001' not in q['allowed_user_ids'])
    assert selected_question(restricted['question_id'],restricted['question'],LOCAL_IDENTITIES['u_employee_001'],None)[0] is None
