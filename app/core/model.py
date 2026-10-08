"""Provider-neutral model contract."""

from dataclasses import dataclass, field
from typing import Any, Protocol
from app.core.config import ModelSettings


@dataclass(frozen=True)
class ModelMessage:
    role: str
    content: str
    tool_call_id: str | None = None


@dataclass(frozen=True)
class ToolRequest:
    call_id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ModelTurn:
    content: str | None = None
    tool_requests: list[ToolRequest] = field(default_factory=list)


class ModelClient(Protocol):
    async def complete(self, messages: list[ModelMessage], tools: list[dict[str, Any]], settings: ModelSettings) -> ModelTurn: ...
