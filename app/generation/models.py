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


def grounded_answer(answer: GeneratedAnswer, evidence: list[dict], question: str = "") -> GeneratedAnswer:
    """Render complete selected source passages, keeping all their conditions.

    This guarantees verbatim support for returned text, not relevance/completeness
    of model passage selection or truth of the source itself. Never truncate rules.
    """
    from app.retrieval.lexical import tokenize
    by_id = {h["chunk_id"]: h for h in evidence}
    selected = list(answer.citations)
    sources = {(by_id[cid]["source"], by_id[cid]["version"]) for cid in selected}
    stop = {"什么", "哪些", "如何", "应该", "这个", "那个", "现在", "是否", "时候", "怎样", "需要"}
    query_terms = {t for t in tokenize(question) if t not in stop and not t.isdigit()}
    covered = set(tokenize("\n".join(by_id[cid]["text"] for cid in selected)))
    # Retain explicitly queried, uncovered sections of an already selected source.
    # Only the actual context is eligible: never fetch a new document or read gold labels.
    for hit in evidence:
        if hit["chunk_id"] in selected or (hit["source"], hit["version"]) not in sources:
            continue
        paths = hit.get("citation", {}).get("heading_paths", [])
        headings = " ".join(path[-1] for path in paths if len(path) > 1)
        if (query_terms - covered) & set(tokenize(headings)):
            selected.append(hit["chunk_id"])
            covered.update(tokenize(hit["text"]))
    claims = [{"text": by_id[cid]["text"].strip(), "citations": [cid]}
              for cid in selected]
    return GeneratedClaims(insufficient_context=False, claims=claims).to_answer()


def resolve_citations(answer: GeneratedAnswer, references: dict[str, str]) -> GeneratedAnswer:
    """Resolve short model references to canonical IDs; caller still checks membership.

    Canonical IDs remain accepted for clients using the old wire format, but never
    authorize anything outside this request's actual evidence.
    """
    claims = [{"text": c.text, "citations": list(dict.fromkeys(references.get(cid, cid) for cid in c.citations))}
              for c in answer.claims]
    return GeneratedClaims(insufficient_context=answer.insufficient_context, claims=claims).to_answer()


def parse_generation(raw: str) -> GeneratedAnswer:
    """Reject extra model fields; render only validated claims."""
    return GeneratedClaims.model_validate_json(raw).to_answer()
