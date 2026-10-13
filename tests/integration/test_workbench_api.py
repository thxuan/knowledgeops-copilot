from tests.doubles import HashEmbeddingDouble
from tests.doubles import ExtractiveGenerationDouble
"""Real HTTP + PostgreSQL coverage of trace, source metrics and scoped parameters."""
import json
from dataclasses import replace
from pathlib import Path
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from app.core.config import Settings
from app.core.embedding import EmbeddingSettings
from app.evals.workbench import fixture
from app.main import create_app
from app.generation.provider import GenerationSettings
from conftest import pg_dsn

HEADERS = {'X-Local-User-Id': 'u_engineer_001'}
ROOT = Path(__file__).resolve().parents[2]

class ProtocolDouble(ExtractiveGenerationDouble):
    async def generate(self, messages, settings):
        payload = json.loads(messages[-1]["content"])
        if "history" not in payload:
            return json.dumps({"queries": [payload["question"]]})
        return await super().generate(messages, settings)

@pytest.fixture
def workbench_app():
    url = pg_dsn('workbench')
    app = create_app(Settings(app_env='test',database_url=url,vector_database_url=url,
        embedding=EmbeddingSettings(provider='compatible', model_id='test-embedding', api_key='test-only', dimensions=16,collection='wb-'+uuid4().hex),ingestion_worker_enabled=False,
        generation=GenerationSettings(provider='compatible',model_id='test-real-adapter',api_key='test-only',coverage_review=False)),generation_client=ProtocolDouble())
    app.state.vector_service.client = HashEmbeddingDouble(app.state.settings.embedding)
    with TestClient(app) as client:
        receipts = []
        for name in ['03-tidelink-vpn-guide.md','04-endpoint-compliance.md']:
            path = ROOT / 'sample_data/knowledge_base' / name
            doc = next(d for d in fixture()[2].values() if d['filename'] == name)
            upload = client.post('/documents',headers=HEADERS,files={'file':(name,path.read_bytes())},
                data={'source':'sample_data/knowledge_base/'+name,'version':doc['version']})
            assert upload.status_code == 202, upload.text
            receipt = upload.json()
            app.state.ingestion_worker.process_next()
            assert client.post(f"/documents/{receipt['document_id']}/chunks",headers=HEADERS,json={}).status_code == 200
            assert client.post(f"/documents/{receipt['document_id']}/index",headers=HEADERS).status_code == 200
            receipts.append(receipt)
        yield app, client, receipts


def ask(client, qid='F005', **kwargs):
    q = next(q for q in fixture()[0] if q['question_id'] == qid)
    result = client.post('/workbench/chat',headers=HEADERS,json={'message':q['question'],'question_id':qid,'top_k':3,**kwargs})
    assert result.status_code == 200, result.text
    return result.json()


def test_page_assets_config_and_identity_boundary(workbench_app):
    app,client,_ = workbench_app
    assert client.get('/').status_code == 200
    assert 'workbench.js' in client.get('/').text
    assert client.get('/workbench/assets/workbench.js').status_code == 200
    assert client.get('/workbench/assets/workbench.css').status_code == 200
    assert client.get('/workbench/config').status_code == 401
    config = client.get('/workbench/config',headers=HEADERS).json()
    assert config['generation']['sampling_supported'] is True
    assert 'api_key' not in str(config) and 'base_url' not in str(config)
    assert all(q['question_id'].startswith(('F','X','U')) for q in config['questions']['questions'])
    assert all('expected_answer_points' not in q for q in config['questions']['questions'])
    other = client.get('/workbench/config',headers={'X-Local-User-Id':'u_employee_001'}).json()
    assert 'F010' not in {q['question_id'] for q in other['questions']['questions']}


def test_actual_windows_scoped_config_and_trace_not_persisted(workbench_app):
    app,client,_ = workbench_app
    original = app.state.settings
    result = ask(client,retrieval={'semantic_k':5,'keyword_k':4,'fusion_k':4},generation={'output_limit':512})
    trace = result['retrieval_trace']['stages']
    assert len(trace['vector']) <= 5 and len(trace['keyword']) <= 4 and len(trace['fusion']) <= 4
    assert len(trace['final']) <= 3 and len(trace['evidence']) <= 3
    assert result['effective_config']['generation']['output_limit'] == 512
    assert app.state.settings is original and app.state.chat.settings.output_limit == 2048
    assert result['evaluation']['status'] == 'measured'
    assert result['evaluation']['answer_correctness'] is None
    assert result['corpus_snapshot']['documents']
    history = client.get('/conversations/'+result['conversation_id'],headers=HEADERS).json()
    assert 'retrieval_trace' not in history['messages'][-1]['payload']
    assert history['messages'][-1]['payload']['context']['effective_config']['top_k'] == 3


def test_keyword_only_has_no_semantic_stage_and_real_citations(workbench_app):
    _,client,_ = workbench_app
    result = ask(client,retrieval={'semantic':False,'keyword':True,'rrf':False,'keyword_k':5})
    assert result['retrieval_trace']['score_type'] == 'bm25'
    assert not result['retrieval_trace']['stages']['vector']
    assert result['effective_config']['retrieval']['rrf'] is False
    assert result['citations']
    assert client.get(result['citations'][0]['url'],headers=HEADERS).status_code == 200
    assert client.get(result['citations'][0]['url'],headers={'X-Local-User-Id':'u_employee_001'}).status_code == 404


def test_no_fake_quality_for_free_modified_or_historical_questions(workbench_app):
    _,client,_ = workbench_app
    q = next(q for q in fixture()[0] if q['question_id'] == 'F005')
    for body in [{'message':q['question']}, {'message':q['question']+' 请简短回答','question_id':'F005'}]:
        response = client.post('/workbench/chat',headers=HEADERS,json={'top_k':3,**body}).json()
        assert response['evaluation']['status'] == 'unavailable'
        assert response['evaluation']['stages'] == {}
    first = ask(client)
    followup = ask(client,conversation_id=first['conversation_id'])
    assert followup['evaluation']['status'] == 'unavailable'


def test_cross_document_metrics_and_all_profiles(workbench_app):
    _,client,_ = workbench_app
    for profile in ['ranked','coverage','multi_query']:
        result = ask(client,'X001',retrieval_profile=profile)
        metric = result['evaluation']['stages']['evidence']
        assert result['evaluation']['question_type'] == 'cross_document'
        assert metric['expected_source_count'] == 2
        assert 0 <= metric['source_recall_at_k'] <= 1
        assert isinstance(metric['all_required_sources_recalled'],bool)
        assert result['retrieval_trace']['timings_ms']


def test_stale_standard_sources_disable_quality_metrics(workbench_app):
    app,client,receipts = workbench_app
    doc = receipts[0]['document_id']
    with app.state.ingestion_repository.connection() as db:
        db.execute('UPDATE documents SET version=%s WHERE document_id=%s',('changed',doc))
    result = ask(client)
    assert result['evaluation']['status'] == 'stale_fixture'
    assert result['evaluation']['stages'] == {}
