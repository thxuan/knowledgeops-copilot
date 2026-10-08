"""Strict contract; valid IDs alone do not prove semantic support."""
from pydantic import BaseModel, ConfigDict, Field, model_validator

class Claim(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    text: str = Field(min_length=1, max_length=4000)
    citations: list[str] = Field(min_length=1, max_length=20)

class GeneratedAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    answer: str = Field(min_length=1, max_length=8000)
    citations: list[str] = Field(max_length=20)
    insufficient_context: bool
    claims: list[Claim] = Field(max_length=20)

    @model_validator(mode="after")
    def consistent_claims(self):
        if not self.answer.strip():
            raise ValueError("Blank answer")
        if self.insufficient_context:
            if self.citations or self.claims:
                raise ValueError("Refusal must not assert evidence-backed claims")
        else:
            if not self.claims or any(not c.text.strip() for c in self.claims):
                raise ValueError("Every answer needs cited claims")
            if self.answer != "\n".join(c.text for c in self.claims):
                raise ValueError("Answer must consist of the validated claims")
            ids = {cid for c in self.claims for cid in c.citations}
            if ids != set(self.citations) or len(self.citations) != len(set(self.citations)):
                raise ValueError("Citation list must match claim citations")
        return self

REFUSAL = "当前授权知识库提供的证据不足，无法回答这个问题。请补充问题或相关文档。"


class GeneratedClaims(BaseModel):
    """Model wire contract: only facts with citations and an explicit refusal flag."""
    model_config = ConfigDict(extra="forbid", strict=True)
    insufficient_context: bool
    claims: list[Claim] = Field(max_length=20)

    @model_validator(mode="after")
    def valid_claims(self):
        if self.insufficient_context:
            if self.claims:
                raise ValueError("Refusal must not assert evidence-backed claims")
        elif not self.claims or any(not claim.text.strip() for claim in self.claims):
            raise ValueError("Every answer needs cited claims")
        return self

    def to_answer(self) -> GeneratedAnswer:
        # Validate the canonical response's length and citation limits as well.
        if self.insufficient_context:
            return GeneratedAnswer(answer=REFUSAL, citations=[], insufficient_context=True, claims=[])
        citations = list(dict.fromkeys(cid for claim in self.claims for cid in claim.citations))
        return GeneratedAnswer(answer="\n".join(claim.text for claim in self.claims),
                               citations=citations, insufficient_context=False, claims=self.claims)


def parse_generation(raw: str) -> GeneratedAnswer:
    """Reject extra model fields; render only validated claims."""
    return GeneratedClaims.model_validate_json(raw).to_answer()
