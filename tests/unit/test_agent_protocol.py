import asyncio
import json
from dataclasses import replace
import httpx
import pytest
from app.agent.contracts import AgentSettings, SPECS, redact
from app.agent.provider import CompatibleAgentModel, Retryable
from app.core.errors import AppError
from app.core.identity import Identity
from app.core.lifecycle import ChatAdmission

S=AgentSettings(provider='compatible',model_id='test',api_key='test-key',input_price=1,output_price=2)

def test_wire_preserves_assistant_call_and_corresponding_result():
    messages=[{'role':'user','content':'计算 2+3'},
              {'role':'assistant','content':None,'tool_calls':[{'id':'call-1','type':'function','function':{'name':'calculator','arguments':'{"left":2,"right":3,"operator":"+"}'}}]},
              {'role':'tool','tool_call_id':'call-1','content':'5'}]
    def handler(request):
        data=json.loads(request.content)
        sent=data['messages']
        expected=json.loads(json.dumps(messages))
        for history in (sent,expected):
            for message in history:
                for call in message.get('tool_calls',[]):
                    call['function']['arguments']=json.loads(call['function']['arguments'])
        assert sent==expected
        assert data['tools']==SPECS
        return httpx.Response(200,json={'model':'test','choices':[{'finish_reason':'stop','message':{'role':'assistant','content':'{"status":"completed","answer":"5"}'}}],
                                      'usage':{'prompt_tokens':100,'completion_tokens':10}})
    result=asyncio.run(CompatibleAgentModel(S,httpx.MockTransport(handler)).complete(messages,SPECS))
    assert result['usage']['prompt_tokens']==100

@pytest.mark.parametrize('status,retry',[(429,True),(503,True),(401,False),(400,False)])
def test_transport_retry_classification(status,retry):
    transport=httpx.MockTransport(lambda r:httpx.Response(status,headers={'Retry-After':'1'}))
    with pytest.raises(Retryable if retry else AppError):
        asyncio.run(CompatibleAgentModel(S,transport).complete([],SPECS))

@pytest.mark.parametrize('body',[{}, {'choices':[]}, {'choices':[{'finish_reason':'length','message':{}}]}])
def test_invalid_model_response(body):
    with pytest.raises(AppError):
        asyncio.run(CompatibleAgentModel(S,httpx.MockTransport(lambda r:httpx.Response(200,json=body))).complete([],SPECS))

def test_redaction():
    assert redact('Bearer abcdefghijkl sk-abcdefghijk a@example.com 13812345678')=='[REDACTED] [REDACTED] [EMAIL] [PHONE]'

@pytest.mark.parametrize('kwargs',[{'max_seconds':float('nan')},{'max_cost':-1},{'input_price':float('inf')},{'output_limit':40000}])
def test_invalid_settings(kwargs):
    with pytest.raises(ValueError): replace(S,**kwargs).validate()

def test_chat_disconnect_cancels_upstream():
    cancelled=[]
    class Request:
        async def receive(self): return {"type":"http.disconnect"}
    async def work():
        try: await asyncio.sleep(10)
        finally: cancelled.append(True)
    async def run():
        guard=ChatAdmission()
        with pytest.raises(AppError) as e:
            await guard.run(Request(),Identity('u','t',frozenset()),work)
        assert e.value.code=='CLIENT_DISCONNECTED' and guard.active==0
    asyncio.run(run())
    assert cancelled

def test_chat_rate_admission():
    class Request:
        async def receive(self):
            await asyncio.sleep(10)
            return {"type":"http.disconnect"}
    async def run():
        guard=ChatAdmission(rate=1)
        identity=Identity('u','t',frozenset())
        assert await guard.run(Request(),identity,lambda:asyncio.sleep(0,result=1))==1
        with pytest.raises(AppError):
            await guard.run(Request(),identity,lambda:asyncio.sleep(0))
    asyncio.run(run())

@pytest.mark.parametrize('content,reason',[
    ('计算结果为40。','not_json'),
    ('{"answer":"40"}','schema_invalid'),
    ('{"status":"completed","answer":"   "}','blank_answer'),
    (None,'missing_text'),
    ('{"status":"completed","answer":"40","approved":true}','schema_invalid'),
])
def test_final_contract_returns_only_safe_diagnostics(content,reason):
    from app.agent.contracts import parse_final
    assert parse_final(content)==(None,reason)

def test_final_repair_requests_json_mode_without_tools():
    from app.agent.contracts import FINAL_REPAIR_PROMPT
    def handler(request):
        body=json.loads(request.content)
        assert 'tools' not in body
        assert body['response_format']=={'type':'json_object'}
        assert 'JSON' in body['messages'][-1]['content']
        return httpx.Response(200,json={'model':'test','choices':[{'finish_reason':'stop','message':{
            'role':'assistant','content':'{"status":"completed","answer":"40"}'}}]})
    result=asyncio.run(CompatibleAgentModel(S,httpx.MockTransport(handler)).complete(
        [{'role':'user','content':FINAL_REPAIR_PROMPT}],[]))
    assert '40' in result['message']['content']
