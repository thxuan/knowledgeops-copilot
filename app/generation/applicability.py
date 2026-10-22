"""Validate model-declared prerequisites against actual in-budget user records.

The model identifies semantic applicability; this gate enforces its declarations.
Documentation and previous assistant claims cannot confirm a user condition.
"""
import json
import unicodedata
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from app.generation.models import Claim, FollowUpModel, GeneratedClaims


class ApplicabilityValidationError(ValueError):
    """Fixed error and server-built field path; never include user/model text."""
    def __init__(self, message, field, detail):
        super().__init__(message)
        self.field = field
        self.detail = detail


class Condition(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(min_length=1, max_length=80)
    value: str | None = Field(max_length=120)
    status: Literal["confirmed", "missing", "mismatched"]
    user_quote: str | None = Field(max_length=500)
    question: str | None = Field(min_length=1, max_length=240)


class ClaimScope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    scope: Literal["general", "conditional"]
    conditions: list[str] = Field(max_length=10)


class Applicability(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    evidence_status: Literal["relevant", "insufficient"]
    conditions: list[Condition] = Field(max_length=10)
    claim_scopes: list[ClaimScope] = Field(max_length=20)


class AssessedClaims(FollowUpModel):
    # Parse the assessment before deriving the final answer/refusal contract.
    model_config = ConfigDict(extra="forbid", strict=True)
    applicability: Applicability
    insufficient_context: bool
    claims: list[Claim] = Field(max_length=20)


def normalized(text):
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def validate_clarification_scope(answer):
    if answer.follow_up and (len(answer.claims) > 2 or sum(len(c.text) for c in answer.claims) > 400):
        raise ValueError("Clarification exceeds scope")
    return answer


def parse_assessed_generation(raw, messages):
    output = AssessedClaims.model_validate_json(raw)
    assessment = output.applicability
    if len(assessment.claim_scopes) != len(output.claims):
        raise ValueError("Claim applicability mismatch")
    payload = json.loads(messages[1]["content"])
    user_texts = [payload["question"]]
    user_texts.extend(row["text"] for row in payload.get("history", []))
    user_texts.extend(row["text"] for row in payload.get("summary", []))
    user_texts = [normalized(text) for text in user_texts]
    conditions, missing = {}, []
    for index, item in enumerate(assessment.conditions):
        field = f"applicability.conditions[{index}]"
        key = normalized(item.name)
        if not key or key in conditions:
            raise ApplicabilityValidationError("Claim applicability mismatch", field + ".name", "empty_or_duplicate_name")
        conditions[key] = item
        if item.status == "confirmed":
            quote = normalized(item.user_quote or "")
            if not quote:
                raise ApplicabilityValidationError("Condition not confirmed by user", field + ".user_quote", "missing_user_quote")
            if not any(quote in text for text in user_texts):
                raise ApplicabilityValidationError("Condition not confirmed by user", field + ".user_quote", "quote_not_in_user_records")
            if not normalized(item.value or ""):
                raise ApplicabilityValidationError("Invalid confirmed condition value", field + ".value", "empty_confirmed_value")
            # Exact quotation establishes provenance. The model's normalized
            # condition value is semantic data, not a second verbatim quotation.
            # Semantic equivalence remains the model's responsibility.
        elif item.status == "missing":
            if not item.question or not item.question.strip():
                raise ApplicabilityValidationError("Claim applicability mismatch", field + ".question", "missing_clarification_question")
            missing.append(item.question.strip())
    # Prioritize the model's ordered prerequisite questions over an unrelated follow-up.
    follow_up = " ".join(list(dict.fromkeys(missing))[:2]) if missing else None
    retained = []
    for claim, scope in zip(output.claims, assessment.claim_scopes):
        if scope.scope == "general":
            if scope.conditions:
                raise ValueError("Claim applicability mismatch")
            retained.append(claim)
            continue
        if not scope.conditions:
            raise ValueError("Claim applicability mismatch")
        required = [conditions.get(normalized(name)) for name in scope.conditions]
        if any(item is None for item in required):
            raise ValueError("Claim applicability mismatch")
        # Drop only conclusions whose own prerequisites are unconfirmed or false.
        # Another pending condition does not invalidate a supported conclusion.
        if all(item.status == "confirmed" for item in required):
            retained.append(claim)
    if assessment.evidence_status == "insufficient":
        if missing or retained:
            raise ValueError("Evidence decision mismatch")
        return GeneratedClaims(insufficient_context=True, claims=[]).to_answer()
    if follow_up:
        # Missing user conditions take precedence over the model's refusal flag.
        answer = GeneratedClaims(insufficient_context=False, claims=retained, follow_up=follow_up).to_answer()
        return validate_clarification_scope(answer)
    if output.follow_up:
        # Every clarification must identify the condition it is meant to resolve.
        raise ValueError("Evidence decision mismatch")
    if retained:
        if output.insufficient_context:
            raise ValueError("Evidence decision mismatch")
        return GeneratedClaims(insufficient_context=False, claims=retained).to_answer()
    if output.claims and any(item.status == "mismatched" for item in assessment.conditions):
        return GeneratedClaims(insufficient_context=True, claims=[]).to_answer()
    raise ValueError("Evidence decision mismatch")
