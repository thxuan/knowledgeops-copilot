"""The fake model proposes a calculator call; the server validates and executes it."""

import re
from dataclasses import dataclass
from decimal import Decimal, DivisionByZero, InvalidOperation
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.config import ModelSettings
from app.core.errors import AppError
from app.core.model import ModelClient, ModelMessage, ModelTurn, ToolRequest


class CalculatorArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    left: Decimal = Field(ge=-1_000_000, le=1_000_000)
    operator: Literal["+", "-", "*", "/"]
    right: Decimal = Field(ge=-1_000_000, le=1_000_000)


def calculate(args: CalculatorArguments) -> str:
    try:
        result = {
            "+": lambda: args.left + args.right,
            "-": lambda: args.left - args.right,
            "*": lambda: args.left * args.right,
            "/": lambda: args.left / args.right,
        }[args.operator]()
    except (DivisionByZero, InvalidOperation, ZeroDivisionError) as exc:
        raise AppError("INVALID_CALCULATION", "Calculation cannot be completed", 422) from exc
    return format(result.normalize(), "f")


CALC_PATTERN = re.compile(
    r"^\s*(?:calc\s*:|计算\s*[:：]?)\s*(-?\d+(?:\.\d+)?)\s*([+*/-])\s*(-?\d+(?:\.\d+)?)\s*$",
    re.IGNORECASE,
)


class FakeModel:
    async def complete(self, messages: list[ModelMessage], tools: list[dict], settings: ModelSettings) -> ModelTurn:
        last = messages[-1]
        if last.role == "tool":
            return ModelTurn(content=f"计算结果：{last.content}")
        match = CALC_PATTERN.fullmatch(last.content)
        if match:
            return ModelTurn(tool_requests=[ToolRequest(
                call_id="fake-call-1", name="calculator",
                arguments={"left": match.group(1), "operator": match.group(2), "right": match.group(3)},
            )])
        return ModelTurn(content="Day 2 尚未接入知识库检索；目前只能演示“计算 2 + 3”这类工具调用。")


CALCULATOR_SPEC = {
    "name": "calculator",
    "description": "Calculate one arithmetic operation on two numbers",
    "parameters": {
        "type": "object",
        "properties": {
            "left": {"type": "number"},
            "operator": {"type": "string", "enum": ["+", "-", "*", "/"]},
            "right": {"type": "number"},
        },
        "required": ["left", "operator", "right"],
        "additionalProperties": False,
    },
}


@dataclass(frozen=True)
class AgentResult:
    answer: str
    tool_calls: list[dict[str, str]]


class AgentRunner:
    def __init__(self, model: ModelClient, settings: ModelSettings, max_steps: int = 3) -> None:
        self.model = model
        self.settings = settings
        self.max_steps = max_steps

    async def run(self, user_message: str) -> AgentResult:
        messages = [ModelMessage(role="user", content=user_message)]
        calls: list[dict[str, str]] = []
        for _ in range(self.max_steps):
            turn = await self.model.complete(messages, [CALCULATOR_SPEC], self.settings)
            if turn.tool_requests:
                if turn.content is not None or len(turn.tool_requests) != 1:
                    raise AppError("INVALID_MODEL_OUTPUT", "Unsupported model tool response", 502)
                request = turn.tool_requests[0]
                if request.name != "calculator":
                    raise AppError("UNKNOWN_TOOL", "Model requested an unavailable tool", 502)
                try:
                    arguments = CalculatorArguments.model_validate(request.arguments)
                except ValidationError as exc:
                    raise AppError("INVALID_TOOL_ARGUMENTS", "Model supplied invalid tool arguments", 502) from exc
                result = calculate(arguments)
                calls.append({"call_id": request.call_id, "name": request.name, "result": result})
                messages.append(ModelMessage(role="tool", content=result, tool_call_id=request.call_id))
                continue
            if turn.content is None:
                raise AppError("INVALID_MODEL_OUTPUT", "Model returned no answer", 502)
            return AgentResult(answer=turn.content, tool_calls=calls)
        raise AppError("STEP_LIMIT", "Agent step limit reached", 502)
