"""Model migration acceptance: exercise LangChain against offline wire fixtures."""
import asyncio
from dataclasses import replace
import json

import httpx
import pytest
from langsmith.run_helpers import get_tracing_context

from app.agent.contracts import AgentSettings, SPECS
from app.core.errors import AppError
from app.core.langchain_models import LangChainAgentModel, LangChainGenerationClient, Retryable
from app.generation.provider import GenerationSettings

GENERATION = GenerationSettings(provider='compatible', model_id='fixture', api_key='test-key', temperature=.7, top_p=.4)
AGENT = AgentSettings(provider='compatible', model_id='fixture', api_key='test-key', input_price=1, output_price=2)


def response(content='{}', *, usage=None, finish='stop', calls=None):
    message = {'role': 'assistant', 'content': content}
    if calls is not None:
        message['tool_calls'] = calls
    body = {'model': 'fixture-version', 'choices': [{'index': 0, 'finish_reason': finish, 'message': message}]}
    if usage is not None:
        body['usage'] = usage
    return body


def test_generation_preserves_wire_sampling_usage_and_json_contract():
    messages = [{'role': 'system', 'content': 'Return JSON'}, {'role': 'user', 'content': '原问题'}]
    usage = {'prompt_tokens': 25, 'completion_tokens': 5, 'total_tokens': 30}
    requests = []
    def handler(request):
        body = json.loads(request.content)
        for key, expected in {'model': 'fixture', 'messages': messages, 'temperature': .7,
                              'top_p': .4, 'max_tokens': 2048, 'response_format': {'type': 'json_object'}}.items():
            assert body[key] == expected
        assert 'max_completion_tokens' not in body
        assert request.headers['authorization'] == 'Bearer test-key'
        requests.append(body)
        return httpx.Response(200, json=response(usage=usage))
    async def run():
        migrated = await LangChainGenerationClient(httpx.MockTransport(handler)).generate(messages, GENERATION)
        assert migrated.text == '{}'
        assert migrated.metadata == {
            'returned_model': 'fixture-version', 'usage': usage,
            'sent_sampling': {'temperature': .7, 'top_p': .4},
            'effective_sampling': 'not echoed by provider'}
    asyncio.run(run())
    assert len(requests) == 1


@pytest.mark.parametrize('usage', [None, {'prompt_tokens': 25}, {'prompt_tokens': 25, 'completion_tokens': 5}])
def test_agent_does_not_invent_missing_billable_usage(usage):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=response(usage=usage)))
    async def run():
        after = await LangChainAgentModel(AGENT, transport).complete([], SPECS)
        assert after['message'] == {'role': 'assistant', 'content': '{}'}
        assert after['model'] == 'fixture-version'
        assert after['usage'] == (usage or {})
    asyncio.run(run())


@pytest.mark.parametrize('counter', [-1, True, '5'])
def test_agent_rejects_malformed_provider_counters(counter):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=response(usage={'prompt_tokens': counter})))
    with pytest.raises(AppError) as error:
        asyncio.run(LangChainAgentModel(AGENT, transport).complete([], SPECS))
    assert error.value.code == 'INVALID_MODEL_OUTPUT'


@pytest.mark.parametrize('status', [429, 500, 502, 503, 504])
def test_agent_leaves_paid_retries_to_the_budgeted_executor(status):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(status, headers={'retry-after': '15'}, json={'error': {'message': 'private provider body'}})
    with pytest.raises(Retryable) as error:
        asyncio.run(LangChainAgentModel(AGENT, httpx.MockTransport(handler)).complete([], SPECS))
    assert error.value.retry_after == 10
    assert len(requests) == 1


def test_tool_history_preserves_call_result_pairing_and_server_owned_schemas():
    call = {'id': 'call-1', 'type': 'function', 'function': {'name': 'calculator', 'arguments': '{"left":2,"right":3,"operator":"+"}'}}
    messages = [{'role': 'user', 'content': '计算2+3'}, {'role': 'assistant', 'content': None, 'tool_calls': [call]},
                {'role': 'tool', 'tool_call_id': 'call-1', 'content': '5'}]
    def handler(request):
        wire = json.loads(request.content)
        assert wire['tools'] == SPECS
        assert wire['messages'][1]['tool_calls'][0]['id'] == 'call-1'
        assert json.loads(wire['messages'][1]['tool_calls'][0]['function']['arguments']) == json.loads(call['function']['arguments'])
        assert wire['messages'][2] == messages[2]
        return httpx.Response(200, json=response(None, finish='tool_calls', calls=[call]))
    result = asyncio.run(LangChainAgentModel(AGENT, httpx.MockTransport(handler)).complete(messages, SPECS))
    assert result['message']['tool_calls'] == [call]


def test_final_repair_disables_tools_and_keeps_json_mode():
    def handler(request):
        wire = json.loads(request.content)
        assert 'tools' not in wire
        assert wire['response_format'] == {'type': 'json_object'}
        return httpx.Response(200, json=response('{"status":"completed","answer":"5"}'))
    result = asyncio.run(LangChainAgentModel(AGENT, httpx.MockTransport(handler)).complete(
        [{'role': 'user', 'content': 'Return JSON'}], []))
    assert json.loads(result['message']['content'])['answer'] == '5'


@pytest.mark.parametrize('finish', ['length', 'content_filter', 'tool_calls'])
def test_incomplete_generation_is_never_accepted(finish):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=response(finish=finish)))
    with pytest.raises(AppError) as error:
        asyncio.run(LangChainGenerationClient(transport).generate([], GENERATION))
    assert error.value.code == 'GENERATION_FAILED'


class Stream(httpx.AsyncByteStream):
    def __init__(self, seen, finish='stop', pending=False):
        self.seen, self.finish, self.pending, self.closed = seen, finish, pending, False
        self.consumed = False

    async def __aiter__(self):
        if self.consumed:
            return
        self.consumed = True
        for text in ('{"answer":"', '中文"}'):
            yield ('data: ' + json.dumps({'model': 'fixture-version', 'choices': [{'index': 0, 'delta': {'content': text}, 'finish_reason': None}]}) + '\n\n').encode()
            assert self.seen[-1] == text
            if self.pending:
                await asyncio.Event().wait()
        yield ('data: ' + json.dumps({'model': 'fixture-version', 'choices': [{'index': 0, 'delta': {}, 'finish_reason': self.finish}]}) + '\n\n').encode()
        yield b'data: {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":20,"total_tokens":30}}\n\n'
        yield b'data: [DONE]\n\n'

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize('finish', ['stop', 'length'])
def test_stream_previews_before_completion_and_requires_successful_finish(finish):
    async def run():
        seen = []
        stream = Stream(seen, finish)
        def handler(request):
            wire = json.loads(request.content)
            assert wire['stream_options'] == {'include_usage': True}
            return httpx.Response(200, stream=stream)
        async def delta(text):
            seen.append(text)
        client = LangChainGenerationClient(httpx.MockTransport(handler))
        if finish == 'stop':
            result = await client.generate_stream([], GENERATION, delta)
            assert result.text == ''.join(seen)
            assert result.metadata['usage'] == {'prompt_tokens': 10, 'completion_tokens': 20, 'total_tokens': 30}
        else:
            with pytest.raises(AppError):
                await client.generate_stream([], GENERATION, delta)
        assert stream.closed
    asyncio.run(run())


def test_stream_cancellation_closes_provider_response():
    async def run():
        seen, started = [], asyncio.Event()
        stream = Stream(seen, pending=True)
        async def delta(text):
            seen.append(text)
            started.set()
        client = LangChainGenerationClient(httpx.MockTransport(lambda request: httpx.Response(200, stream=stream)))
        task = asyncio.create_task(client.generate_stream([], GENERATION, delta))
        await asyncio.wait_for(started.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stream.closed
    asyncio.run(run())


def test_generic_langsmith_environment_does_not_enable_cloud_tracing(monkeypatch):
    monkeypatch.setenv('LANGSMITH_TRACING', 'true')
    monkeypatch.delenv('KNOWLEDGEOPS_LANGSMITH_TRACING', raising=False)
    def handler(request):
        assert get_tracing_context()['enabled'] is False
        return httpx.Response(200, json=response())
    result = asyncio.run(LangChainGenerationClient(httpx.MockTransport(handler)).generate([], GENERATION))
    assert result.text == '{}'


@pytest.mark.parametrize('kind', ['event', 'text'])
def test_stream_preserves_event_and_utf8_output_limits(kind):
    class Excessive(httpx.AsyncByteStream):
        closed = False
        async def __aiter__(self):
            part = {'choices': [{'index': 0, 'delta': {'content': '中' * 60 if kind == 'text' else 'x'}, 'finish_reason': None}]}
            if kind == 'event':
                part['padding'] = 'x' * 600
            yield ('data: ' + json.dumps(part, ensure_ascii=False) + '\n\n').encode()
        async def aclose(self):
            self.closed = True
    async def run():
        upstream, seen = Excessive(), []
        async def delta(text):
            seen.append(text)
        settings = replace(GENERATION, output_limit=8)
        client = LangChainGenerationClient(httpx.MockTransport(lambda request: httpx.Response(200, stream=upstream)))
        with pytest.raises(AppError) as error:
            await client.generate_stream([], settings, delta)
        assert error.value.code == 'GENERATION_FAILED'
        assert seen == [] and upstream.closed
    asyncio.run(run())
