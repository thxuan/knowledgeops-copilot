"""Strict contract; valid IDs alone do not prove semantic support."""
from pydantic import BaseModel, ConfigDict, Field, model_validator, field_validator

class Claim(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    text: str = Field(min_length=1, max_length=4000)
    citations: list[str] = Field(min_length=1, max_length=20)

class FollowUpModel(BaseModel):
    follow_up: str | None = Field(default=None, min_length=1, max_length=500)

    @field_validator('follow_up', mode='before')
    @classmethod
    def normalize_follow_up(cls, value):
        return value.strip() or None if isinstance(value, str) else value


class GeneratedAnswer(FollowUpModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    answer: str = Field(max_length=8000)
    citations: list[str] = Field(max_length=20)
    insufficient_context: bool
    claims: list[Claim] = Field(max_length=20)

    @model_validator(mode="after")
    def consistent_claims(self):
        if not self.answer.strip() and not (self.follow_up and not self.claims and not self.insufficient_context):
            raise ValueError("Blank answer")
        if self.insufficient_context:
            if self.citations or self.claims or self.follow_up:
                raise ValueError("Refusal must not assert evidence-backed claims")
        else:
            if (not self.claims and not self.follow_up) or any(not c.text.strip() for c in self.claims):
                raise ValueError("Every answer needs cited claims")
            if self.answer != "\n".join(c.text for c in self.claims):
                raise ValueError("Answer must consist of the validated claims")
            ids = {cid for c in self.claims for cid in c.citations}
            if ids != set(self.citations) or len(self.citations) != len(set(self.citations)):
                raise ValueError("Citation list must match claim citations")
        return self

VALIDATION_FAILURE = "本次回答未通过校验，暂未生成可用答复。请重试或继续补充问题。"

REFUSAL = "当前授权知识库提供的证据不足，无法回答这个问题。请补充问题或相关文档。"


class GeneratedClaims(FollowUpModel):
    """Model wire contract: only facts with citations and an explicit refusal flag."""
    model_config = ConfigDict(extra="forbid", strict=True)
    insufficient_context: bool
    claims: list[Claim] = Field(max_length=20)

    @model_validator(mode="after")
    def valid_claims(self):
        if self.insufficient_context:
            if self.claims or self.follow_up:
                raise ValueError("Refusal must not assert evidence-backed claims")
        elif (not self.claims and not self.follow_up) or any(not claim.text.strip() for claim in self.claims):
            raise ValueError("Every answer needs cited claims")
        return self

    def to_answer(self) -> GeneratedAnswer:
        # Validate the canonical response's length and citation limits as well.
        if self.insufficient_context:
            return GeneratedAnswer(answer=REFUSAL, citations=[], insufficient_context=True, claims=[])
        citations = list(dict.fromkeys(cid for claim in self.claims for cid in claim.citations))
        return GeneratedAnswer(answer="\n".join(claim.text for claim in self.claims),
                               citations=citations, insufficient_context=False, claims=self.claims, follow_up=self.follow_up)


def resolve_citations(answer: GeneratedAnswer, references: dict[str, str]) -> GeneratedAnswer:
    """Resolve short model references to canonical IDs; caller still checks membership.

    Canonical IDs remain accepted for clients using the old wire format, but never
    authorize anything outside this request's actual evidence.
    """
    claims = [{"text": c.text, "citations": list(dict.fromkeys(references.get(cid, cid) for cid in c.citations))}
              for c in answer.claims]
    return GeneratedClaims(insufficient_context=answer.insufficient_context, claims=claims, follow_up=answer.follow_up).to_answer()


def parse_generation(raw: str) -> GeneratedAnswer:
    """Reject extra model fields; render only validated claims."""
    return GeneratedClaims.model_validate_json(raw).to_answer()
