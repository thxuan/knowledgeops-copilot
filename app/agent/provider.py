"""Chat-completions tool protocol; no SDK or framework magic."""
from uuid import uuid4
import httpx
from app.agent.contracts import dump
from app.core.errors import AppError

class Retryable(Exception):
    def __init__(self, retry_after=0):
        self.retry_after = min(10, max(0, retry_after))

class CompatibleAgentModel:
    def __init__(self, settings, transport=None):
        self.settings, self.transport = settings, transport

    async def complete(self, messages, tools):
        s = self.settings
        try:
            async with httpx.AsyncClient(timeout=min(30, s.max_seconds), transport=self.transport) as client:
                payload = {"model": s.model_id, "messages": messages,
                           "temperature": 0, "max_tokens": s.output_limit}
                if tools:
                    payload["tools"] = tools
                else:
                    # A dedicated final-format repair request cannot execute tools.
                    payload["response_format"] = {"type": "json_object"}
                response = await client.post(s.base_url.rstrip("/") + "/chat/completions",
                    headers={"Authorization": "Bearer " + s.api_key}, json=payload)
            if response.status_code in {429, 500, 502, 503, 504}:
                try:
                    delay = float(response.headers.get("retry-after", "0"))
                except ValueError:
                    delay = 0
                raise Retryable(delay)
            if response.is_error:
                raise AppError("MODEL_REJECTED", "Model request rejected", 502)
            if len(response.content) > s.output_limit*32+65536:
                raise ValueError("Oversized response")
            data = response.json()
            choice = data["choices"][0]
            if choice["finish_reason"] not in {"stop", "tool_calls"}:
                raise ValueError("Incomplete response")
            message = choice["message"]
            if message.get("role") != "assistant":
                raise ValueError("Invalid role")
            usage = data.get("usage") or {}
            usage = {k: usage[k] for k in ("prompt_tokens", "completion_tokens", "total_tokens") if k in usage}
            if any(type(v) is not int or v < 0 for v in usage.values()):
                raise ValueError("Invalid usage counters")
            return {"message": {k: message[k] for k in ("role", "content", "tool_calls") if k in message},
                    "usage": usage, "model": data.get("model", s.model_id)}
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise Retryable() from exc
        except (ValueError, KeyError, TypeError, IndexError) as exc:
            raise AppError("INVALID_MODEL_OUTPUT", "Malformed tool protocol", 502) from exc

def tool_message(name, arguments):
    return {"role": "assistant", "content": None, "tool_calls": [{"id": uuid4().hex,
        "type": "function", "function": {"name": name, "arguments": dump(arguments)}}]}


class UnconfiguredAgentModel:
    async def complete(self, messages, tools):
        raise AppError('AGENT_MODEL_NOT_CONFIGURED', '请配置真实 Agent 模型和输入、输出单价。', 422)
