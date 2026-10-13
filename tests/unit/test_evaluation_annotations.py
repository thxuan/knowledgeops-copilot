"""Regression checks for complete held-out labels before any paid evaluation."""
import asyncio
from copy import deepcopy
from app.core.embedding import EmbeddingSettings
from app.generation.provider import GenerationSettings
import json
from pathlib import Path
import pytest
from app.evals.chunk_comparison import resolve_anchors
from app.evals.retrieval import measure, validate_annotations
from app.core.config import Settings
from scripts.compare_chunks import load_corpus
from scripts.evaluate_rag import load_evaluation_data, read_jsonl, validate_frozen_config

ROOT = Path(__file__).resolve().parents[2]


def tiny_data():
    corpus = {'doc': {'text': '# Entry process\n\n## 1. Entry\nactivate at eight\n\n## 2. Exit\nrevoke\n',
        'title': 'Entry process', 'filename': 'entry.md', 'version': 'v1', 'parser_version': 'fixture'}}
    question = {'question_id': 'held', 'split': 'test', 'answerable': True,
                'expected_sources': [{'document_id': 'doc', 'version': 'v1', 'section': '1. Entry'}]}
    return corpus, question


def test_holdout_annotation_resolver_prefers_exact_section_over_document_prefix():
    corpus, q = tiny_data()
    assert resolve_anchors(corpus, [q]) == []
    anchors = resolve_anchors(corpus, [q], split='test')
    assert len(anchors) == 1
    assert anchors[0]['start_char'] == corpus['doc']['text'].index('## 1. Entry')
    validate_annotations([q], anchors, corpus)
    hit = {'source': 'entry', 'citation': {'version': 'v1', 'text_hash': anchors[0]['text_hash'],
        'parser_version': 'fixture', 'location': {'start_char': anchors[0]['start_char'],
                                               'end_char': anchors[0]['end_char']}}}
    assert measure(q, [hit], anchors, {'entry':'doc'})['source_recall_at_k'] == 1


def test_ambiguous_prefix_is_not_silently_resolved_to_first_section():
    corpus, q = tiny_data()
    corpus['doc']['text'] = '# Guide\n\n## 1. Entry alpha\na\n\n## 2. Entry beta\nb\n'
    with pytest.raises(ValueError, match='Ambiguous source section'):
        resolve_anchors(corpus, [q], split='test')


def test_full_fixture_has_all_dev_and_heldout_source_sections():
    corpus = load_corpus()
    questions = read_jsonl(ROOT / 'eval_data/questions.jsonl')
    anchors = read_jsonl(ROOT / 'eval_data/day4-source-spans.jsonl')
    validate_annotations(questions, anchors, corpus)
    for split, answerable_count in [('dev',17),('test',8)]:
        _, selected, loaded = load_evaluation_data(split)
        assert len(selected) == (20 if split == 'dev' else 10)
        assert sum(q['answerable'] for q in selected) == answerable_count
        assert len({a['question_id'] for a in loaded if a['question_id'] in {q['question_id'] for q in selected}}) == answerable_count


@pytest.mark.parametrize('damage', ['missing_source','missing_section','stale_hash','invalid_coordinate','wrong_section_range','duplicate_section'])
def test_preflight_rejects_incomplete_or_stale_annotations(damage):
    corpus, q = tiny_data()
    anchors = resolve_anchors(corpus, [q], split='test')
    if damage == 'missing_source':
        anchors = []
    elif damage == 'missing_section':
        q['expected_sources'][0]['section'] = '1. Entry；2. Exit'
    elif damage == 'stale_hash':
        anchors[0]['text_hash'] = 'changed'
    elif damage == 'invalid_coordinate':
        anchors[0]['end_char'] = len(corpus['doc']['text']) + 1
    elif damage == 'wrong_section_range':
        anchors[0]['start_char'] += 1
    elif damage == 'duplicate_section':
        anchors += deepcopy(anchors)
    with pytest.raises(ValueError):
        validate_annotations([q], anchors, corpus)


def test_missing_annotation_aborts_before_settings_database_or_models(tmp_path, monkeypatch, capsys):
    import scripts.evaluate_rag as evaluator
    corpus, q = tiny_data()
    (tmp_path/'eval_data').mkdir()
    (tmp_path/'eval_data/questions.jsonl').write_text(json.dumps(q)+'\n',encoding='utf-8')
    (tmp_path/'eval_data/day4-source-spans.jsonl').write_text('',encoding='utf-8')
    monkeypatch.setattr(evaluator,'ROOT',tmp_path)
    monkeypatch.setattr(evaluator,'load_corpus',lambda:corpus)
    observed=[]
    def forbidden_settings():
        observed.append('settings')
        raise AssertionError('Preflight must run before settings/database/model use')
    monkeypatch.setattr(evaluator.Settings,'from_env',forbidden_settings)
    monkeypatch.setattr('sys.argv',['evaluate_rag.py','--database-url','postgresql://fixture/fixture','--split','test','--freeze-from','unused'])
    with pytest.raises(SystemExit) as error:
        asyncio.run(evaluator.main())
    assert error.value.code == 2
    assert 'Missing source annotations: held' in capsys.readouterr().err
    assert not observed and not (tmp_path/'docs').exists()


def test_fatal_run_error_marks_manifest_failed(tmp_path, monkeypatch):
    import scripts.evaluate_rag as evaluator
    corpus,q=tiny_data()
    q['split']='dev'
    anchors=resolve_anchors(corpus,[q])
    (tmp_path/'eval_data').mkdir()
    for name, data in [('questions.jsonl',[q]),('day4-source-spans.jsonl',anchors)]:
        (tmp_path/'eval_data'/name).write_text(''.join(json.dumps(r)+'\n' for r in data),encoding='utf-8')
    monkeypatch.setattr(evaluator,'ROOT',tmp_path)
    monkeypatch.setattr(evaluator,'load_corpus',lambda:corpus)
    monkeypatch.setattr(evaluator.Settings,'from_env',lambda:Settings(vector_database_url='fixture', embedding=EmbeddingSettings(provider='compatible', api_key='test-only'), generation=GenerationSettings(provider='compatible', api_key='test-only')))
    async def abort(*args):
        raise ValueError('Evaluation fixture failure')
    monkeypatch.setattr(evaluator,'run_evaluation',abort)
    monkeypatch.setattr('sys.argv',['evaluate_rag.py','--database-url','postgresql://fixture/fixture'])
    with pytest.raises(ValueError,match='fixture failure'):
        asyncio.run(evaluator.main())
    manifests=list((tmp_path/'docs/experiments/day8').glob('*/manifest.json'))
    assert len(manifests)==1
    manifest=json.loads(manifests[0].read_text(encoding='utf-8'))
    assert manifest['status']=='failed' and manifest['error_code']=='ValueError'


@pytest.mark.parametrize('change', ['model','prompt','code','labels','status','split'])
def test_freeze_gate_remains_strict_after_evaluation_repair(change):
    config={'generation':{'model_id':'qwen-plus'},'prompt_sha256':'prompt',
            'code_and_data_sha256':{'app/retrieval/pipeline.py':'code','eval_data/day4-source-spans.jsonl':'labels'}}
    frozen={'split':'dev','status':'frozen_after_evaluation_repair','config':deepcopy(config)}
    validate_frozen_config(frozen,config)
    if change=='model': frozen['config']['generation']['model_id']='other'
    elif change=='prompt': frozen['config']['prompt_sha256']='other'
    elif change=='code': frozen['config']['code_and_data_sha256']['app/retrieval/pipeline.py']='other'
    elif change=='labels': frozen['config']['code_and_data_sha256']['eval_data/day4-source-spans.jsonl']='other'
    elif change=='status': frozen['status']='running'
    elif change=='split': frozen['split']='test'
    with pytest.raises(ValueError):
        validate_frozen_config(frozen,config)
