import json
from dataclasses import replace
from tests.integration.test_workbench_api import workbench_app, HEADERS, ProtocolDouble
from app.evals.workbench import fixture


def upload(client, rows, spans=None):
    return client.post('/workbench/datasets', headers=HEADERS, json={'filename':'test.jsonl','content':'\n'.join(json.dumps(q) for q in rows),'spans_content':'\n'.join(json.dumps(s) for s in spans or [])})


def ask(client,dataset,q,**extra):
    return client.post('/workbench/chat',headers=HEADERS,json={'message':q['question'],'question_id':q.get('question_id','Q001'),'dataset_id':dataset['id'],'top_k':3,**extra})


def test_plain_and_owner(workbench_app):
    app,client,_=workbench_app
    q={'question':'TL-403 合规刷新需要多久？'}
    uploaded=upload(client,[q]);assert uploaded.status_code==200,uploaded.text
    dataset=uploaded.json();r=ask(client,dataset,q);assert r.status_code==200,r.text
    assert r.json()['evaluation']['stages']=={}
    assert ask(client,dataset,{'question':'changed'}).status_code==422
    assert ask(client,dataset,q,conversation_id=r.json()['conversation_id']).status_code==422
    denied=client.post('/workbench/chat',headers={'X-Local-User-Id':'u_employee_001'},json={'message':q['question'],'question_id':'Q001','dataset_id':dataset['id']})
    assert denied.status_code==404


def test_gold_isolation_and_metrics(workbench_app):
    app,client,_=workbench_app
    original=next(q for q in fixture()[0] if q['question_id']=='F005')
    q={**original,'question_id':'CUSTOM','split':'test','expected_answer_points':['GOLD_SENTINEL']}
    class Recording(ProtocolDouble):
        calls=[]
        async def generate(self,messages,settings):
            self.calls.append(messages)
            return await super().generate(messages,settings)
    recorder=Recording();app.state.chat.client=recorder
    dataset=upload(client,[q]).json();response=ask(client,dataset,q);assert response.status_code==200,response.text
    metric=response.json()['evaluation']['stages']['evidence']
    assert metric['source_recall_at_k']==1 and metric['precision_at_k'] is None
    assert 'GOLD_SENTINEL' not in json.dumps(recorder.calls)
    spans=[{**a,'question_id':'CUSTOM'} for a in fixture()[1] if a['question_id']=='F005']
    dataset=upload(client,[q],spans).json();r=ask(client,dataset,q)
    assert r.json()['evaluation']['stages']['evidence']['precision_at_k'] is not None
    assert 'GOLD_SENTINEL' not in json.dumps(recorder.calls)
    spans[0]['text_hash']='0'*64
    dataset=upload(client,[q],spans).json()
    assert ask(client,dataset,q).json()['evaluation']['status']=='stale_fixture'


def test_validation_runtime_and_example(workbench_app):
    app,client,_=workbench_app
    bad=client.post('/workbench/datasets',headers=HEADERS,json={'filename':'bad.jsonl','content':'{}\nnot json'})
    assert bad.status_code==422 and '2' in bad.json()['error']['message']
    assert upload(client,[{'question_id':'same','question':'a'}]*2).status_code==422
    q={'question':'TL-403 是什么？'};dataset=upload(client,[q]).json()
    fingerprint=client.get('/workbench/datasets/runtime',headers=HEADERS).json()['fingerprint']
    app.state.chat.settings=replace(app.state.chat.settings,model_id='changed-model')
    assert ask(client,dataset,q,runtime_fingerprint=fingerprint).status_code==409
    example=client.get('/workbench/datasets/example',headers=HEADERS).json()
    rows=[json.loads(line) for line in example['content'].splitlines()]
    assert all(q['split']=='dev' for q in rows)
    assert upload(client,rows).status_code==200
