import asyncio
import json
from dataclasses import replace
from tests.integration.test_chat_api import chat_app, ask, Scripted, HEADERS, OTHER, VPN
from app.api.schemas import ChatRequest
from app.core.identity import LOCAL_IDENTITIES


def wire(claims=None,follow_up=None,refused=False,assessed=False):
    if assessed:
        from tests.doubles import assessed_output
        return assessed_output(claims or [],follow_up=follow_up,refused=refused)
    return json.dumps({'insufficient_context':refused,'claims':claims or [],'follow_up':follow_up},ensure_ascii=False)


def test_partial_answer_follow_up_persists_and_short_reply_continues_with_frozen_replay(chat_app):
    app,client,settings,hits=chat_app
    first_output=wire([{'text':VPN,'citations':['E1']}],'你使用什么系统？')
    app.state.chat.client=Scripted([first_output])
    first=ask(client,'我的 VPN 连接失败，怎么排查？')
    assert first['status']=='needs_clarification' and first['answer']==VPN
    assert first['follow_up']=='你使用什么系统？' and len(first['citations'])==1
    identity=LOCAL_IDENTITIES['u_engineer_001'];cid=first['conversation_id']
    snapshot=app.state.conversations.load(cid,identity)
    assert snapshot['pending_follow_up']['original_question']=='我的 VPN 连接失败，怎么排查？'
    history=client.get('/conversations/'+cid,headers=HEADERS).json()
    assert first['follow_up'] in history['messages'][-1]['content']
    context_id,_=app.state.conversations.capture_context(snapshot,'Windows 11',identity)
    frozen=app.state.conversations.replay_context(context_id,'Windows 11',identity)
    async def refuse_for_missing_applicable_evidence(messages,config):
        payload=json.loads(messages[1]['content'])
        assert payload['question']=='Windows 11'
        assert payload['pending_follow_up']['question']==first['follow_up']
        assert 'VPN' in payload['pending_follow_up']['original_question']
        assert payload['history'][0]['text']=='我的 VPN 连接失败，怎么排查？'
        return wire(refused=True)
    app.state.chat.client=Scripted([refuse_for_missing_applicable_evidence]*2)
    replay=asyncio.run(app.state.chat.run(ChatRequest(message='Windows 11'),identity,history_snapshot=frozen,commit_turn=False))
    assert 'Windows 11' in replay['context']['retrieval_query'] and 'VPN' in replay['context']['retrieval_query']
    second=ask(client,'Windows 11',cid)
    assert second['status']=='refused' and second['follow_up'] is None
    assert '补充' in second['answer'] and 'Windows 11' in app.state.chat.vectors.queries[-1]
    assert client.get('/conversations/'+cid,headers=OTHER).status_code==404


def test_question_only_and_empty_evidence_can_clarify_but_cannot_invent_citations(chat_app):
    app,client,_,_=chat_app
    app.state.chat.settings=replace(app.state.chat.settings,provider='compatible',api_key='offline',coverage_review=True)
    app.state.chat.vectors.hits=[]
    app.state.chat.client=Scripted([wire(follow_up='请提供版本和错误提示。',assessed=True)])
    answer=ask(client,'软件启动失败')
    assert answer['status']=='needs_clarification' and answer['answer']=='' and answer['claims']==answer['citations']==[]
    assert answer['follow_up'] and answer['context']['generation_attempts']==1
    assert answer['context']['coverage_review']=='skipped_clarification'
    app.state.chat.client=Scripted([wire([{'text':'编造的修复步骤','citations':['E1']}],follow_up='什么版本？',assessed=True)]*2)
    bad=ask(client,'另一个软件也启动失败')
    assert bad['status']=='rejected' and bad['follow_up'] is None
    assert bad['claims']==bad['citations']==[]


def test_revoked_sources_remove_pending_question_from_history_and_frozen_replay(chat_app):
    app,client,_,hits=chat_app
    app.state.chat.client=Scripted([wire([{'text':VPN,'citations':['E1']}],'请补充系统。')])
    first=ask(client);identity=LOCAL_IDENTITIES['u_engineer_001'];cid=first['conversation_id']
    snapshot=app.state.conversations.load(cid,identity)
    context_id,_=app.state.conversations.capture_context(snapshot,'Windows',identity)
    app.state.document_repository.permissions(hits[0]['document_id'],identity,[])
    assert app.state.conversations.load(cid,identity)['pending_follow_up'] is None
    assert app.state.conversations.replay_context(context_id,'Windows',identity)['pending_follow_up'] is None
    saved=client.get('/conversations/'+cid,headers=HEADERS).json()
    assert saved['messages'][-1]['redacted'] and not saved['messages'][-1]['payload'].get('follow_up')
    app.state.chat.client=Scripted([wire(refused=True)])
    answer=ask(client,'Windows',cid)
    assert answer['context']['retrieval_query']=='Windows'
