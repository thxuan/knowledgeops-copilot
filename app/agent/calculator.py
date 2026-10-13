"""Validated arithmetic tool for the persistent Agent."""
from decimal import Decimal, DivisionByZero, InvalidOperation
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from app.core.errors import AppError

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
