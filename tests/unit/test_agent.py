import asyncio
import pytest
from app.agent.loop import AgentRunner
from app.core.config import ModelSettings
from app.core.errors import AppError
from app.core.model import ModelTurn, ToolRequest


class BadModel:
    async def complete(self, messages, tools, settings):
        return ModelTurn(tool_requests=[ToolRequest("bad-1", "shell", {"command": "whoami"})])


def test_model_cannot_execute_unknown_tool() -> None:
    with pytest.raises(AppError, match="UNKNOWN_TOOL"):
        asyncio.run(AgentRunner(BadModel(), ModelSettings()).run("anything"))
