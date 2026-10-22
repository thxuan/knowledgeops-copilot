"""LangChain model boundary; application validation and transactions stay outside."""
from contextlib import asynccontextmanager
import codecs
from dataclasses import dataclass
import os

import httpx
from langchain_core.messages import AIMessage
from langchain_openai import ChatOpenAI
from langsmith import tracing_context
import openai

from app.core.errors import AppError


class Retryable(Exception):
    def __init__(self, retry_after=0):
        self.retry_after = min(10, max(0, retry_after))


@dataclass
class GenerationResult:
    text: str
    metadata: dict


class _BoundedEventStream(httpx.AsyncByteStream):
    """Keep the existing SSE line limit before the SDK parses event JSON."""
    def __init__(self, upstream, line_limit):
        self.upstream, self.line_limit, self.consumed = upstream, line_limit, False

    async def __aiter__(self):
        if self.consumed:
            return
        self.consumed = True
        decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        line_length = 0
        async for data in self.upstream:
            lines = decoder.decode(data).split('\n')
            for index, line in enumerate(lines):
                line_length += len(line)
                if line_length > self.line_limit:
                    raise ValueError('Excessive stream event')
                if index < len(lines) - 1:
                    line_length = 0
            yield data

    async def aclose(self):
        await self.upstream.aclose()


def _usage(value, *, strict=False):
    value = value or {}
    if not isinstance(value, dict):
        raise ValueError('Invalid usage counters')
    result = {}
    for key in ('prompt_tokens', 'completion_tokens', 'total_tokens'):
        if key in value:
            if type(value[key]) is int and value[key] >= 0:
                result[key] = value[key]
            elif strict:
                raise ValueError('Invalid usage counters')
    return result


@asynccontextmanager
async def _model(*, model_id, base_url, api_key, temperature, top_p,
                 output_limit, timeout, transport, streaming=False, agent=False, json_mode=True):
    # Preserve original counters: SDK normalization must not turn absent usage
    # into billable zeroes or coerce malformed counters before budget checks.
    wire = {}
    streaming_responses = []

    async def capture(response):
        if response.is_error:
            return
        if streaming:
            response.stream = _BoundedEventStream(response.stream, output_limit * 32)
            streaming_responses.append(response)
            return
        await response.aread()
        if agent and len(response.content) > output_limit * 32 + 65536:
            raise ValueError('Oversized response')
        body = response.json()
        wire.update(body)
        _usage(body.get('usage'), strict=agent)
        message = body['choices'][0]['message']
        if agent and message.get('role') != 'assistant':
            raise ValueError('Invalid role')
        if not agent and not isinstance(message.get('content'), str):
            raise ValueError('Invalid generation content')

    # Own both clients so model construction cannot leak a default sync client.
    with httpx.Client(timeout=timeout) as sync_client:
        async with httpx.AsyncClient(timeout=timeout, transport=transport,
                                     event_hooks={'response': [capture]}) as async_client:
            kwargs = {'top_p': top_p} if top_p is not None else {}
            extra_body = {'max_tokens': output_limit}
            if json_mode:
                # Ask the provider for JSON without enabling SDK parsed streaming.
                # Claims and final answers remain subject to application validation.
                extra_body['response_format'] = {'type': 'json_object'}
            model = ChatOpenAI(
                model=model_id, base_url=base_url.rstrip('/'), api_key=api_key,
                temperature=temperature, timeout=timeout, max_retries=0,
                # Compatible providers already accept max_tokens. Supplying it
                # through extra_body avoids SDK renaming to max_completion_tokens.
                extra_body=extra_body,
                http_client=sync_client, http_async_client=async_client,
                use_responses_api=False, stream_usage=streaming, output_version='v0',
                **kwargs,
            )
            # Cloud tracing is a separate migration stage and requires explicit
            # application opt-in, even when generic LANGSMITH_* variables exist.
            enabled = os.getenv('KNOWLEDGEOPS_LANGSMITH_TRACING', 'false').lower() == 'true'
            with tracing_context(enabled=enabled):
                try:
                    yield model, wire
                finally:
                    # Closing a layered astream iterator does not guarantee that
                    # its underlying SDK response closes on an application error.
                    for response in streaming_responses:
                        await response.aclose()


class LangChainGenerationClient:
    def __init__(self, transport=None):
        self.transport = transport

    async def generate(self, messages, settings):
        finish = None
        try:
            async with _model(model_id=settings.model_id, base_url=settings.base_url,
                    api_key=settings.api_key, temperature=settings.temperature, top_p=settings.top_p,
                    output_limit=settings.output_limit, timeout=settings.timeout_seconds,
                    transport=self.transport) as (model, wire):
                reply = await model.ainvoke(
                    messages, config={'run_name': 'knowledgeops.generation'})
            finish = reply.response_metadata.get('finish_reason')
            if finish != 'stop' or not isinstance(reply.content, str):
                raise ValueError('Incomplete generation')
            return GenerationResult(reply.content, self._metadata(settings, wire.get('model'),
                                                                  _usage(wire.get('usage'))))
        except (openai.OpenAIError, httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
            detail = ''
            if isinstance(exc, openai.APIStatusError):
                detail = f' (HTTP {exc.status_code})'
            elif finish in {'length', 'content_filter', 'tool_calls'}:
                detail = f' (finish_reason={finish})'
            elif isinstance(exc, (openai.APITimeoutError, httpx.TimeoutException)):
                detail = ' (timeout)'
            raise AppError('GENERATION_FAILED', 'Provider returned no complete answer' + detail, 502) from exc

    async def generate_stream(self, messages, settings, on_delta):
        text, usage, returned_model, finish = '', {}, None, None
        try:
            async with _model(model_id=settings.model_id, base_url=settings.base_url,
                    api_key=settings.api_key, temperature=settings.temperature, top_p=settings.top_p,
                    output_limit=settings.output_limit, timeout=settings.timeout_seconds,
                    transport=self.transport, streaming=True) as (model, _):
                stream = model.astream(
                    messages, config={'run_name': 'knowledgeops.generation.stream'})
                try:
                    async for chunk in stream:
                        metadata = chunk.response_metadata
                        returned_model = metadata.get('model_name') or returned_model
                        finish = metadata.get('finish_reason') or finish
                        if chunk.usage_metadata:
                            counts = chunk.usage_metadata
                            usage = _usage({'prompt_tokens': counts.get('input_tokens'),
                                            'completion_tokens': counts.get('output_tokens'),
                                            'total_tokens': counts.get('total_tokens')})
                        if not isinstance(chunk.content, str):
                            raise ValueError('Invalid stream content')
                        if chunk.content:
                            text += chunk.content
                            if len(text.encode('utf-8')) > settings.output_limit * 16:
                                raise ValueError('Excessive stream output')
                            await on_delta(chunk.content)
                finally:
                    await stream.aclose()
            if finish != 'stop' or not text:
                raise ValueError('Incomplete generation')
            return GenerationResult(text, {**self._metadata(settings, returned_model, usage), 'transport': 'sse'})
        except (openai.OpenAIError, httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
            raise AppError('GENERATION_FAILED', '流式生成未完整结束，未保存回答。', 502) from exc

    @staticmethod
    def _metadata(settings, returned_model, usage):
        return {'returned_model': returned_model, 'usage': usage,
                'sent_sampling': {'temperature': settings.temperature, 'top_p': settings.top_p},
                'effective_sampling': 'not echoed by provider'}


class LangChainAgentModel:
    def __init__(self, settings, transport=None):
        self.settings, self.transport = settings, transport

    async def complete(self, messages, tools):
        s = self.settings
        try:
            async with _model(model_id=s.model_id, base_url=s.base_url, api_key=s.api_key,
                    temperature=0, top_p=None, output_limit=s.output_limit,
                    timeout=min(30, s.max_seconds), transport=self.transport, agent=True,
                    json_mode=not tools) as (model, wire):
                # Preserve the server-owned schemas. No framework tool execution
                # is enabled here; the business executor is a later migration.
                bound = model.bind(tools=tools) if tools else model
                reply = await bound.ainvoke(messages, config={'run_name': 'knowledgeops.agent.model'})
            if not isinstance(reply, AIMessage) or reply.response_metadata.get('finish_reason') not in {'stop', 'tool_calls'}:
                raise ValueError('Incomplete response')
            message = wire['choices'][0]['message']
            return {'message': {key: message[key] for key in ('role', 'content', 'tool_calls') if key in message},
                    'usage': _usage(wire.get('usage'), strict=True), 'model': wire.get('model', s.model_id)}
        except (openai.APITimeoutError, openai.APIConnectionError, httpx.TimeoutException, httpx.NetworkError) as exc:
            raise Retryable() from exc
        except openai.APIStatusError as exc:
            if exc.status_code in {429, 500, 502, 503, 504}:
                try:
                    delay = float(exc.response.headers.get('retry-after', '0'))
                except ValueError:
                    delay = 0
                raise Retryable(delay) from exc
            raise AppError('MODEL_REJECTED', 'Model request rejected', 502) from exc
        except (openai.OpenAIError, ValueError, KeyError, IndexError, TypeError) as exc:
            raise AppError('INVALID_MODEL_OUTPUT', 'Malformed tool protocol', 502) from exc
