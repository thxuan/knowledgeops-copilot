"""Bounded clause queries and evidence selection using authorized candidates only."""
import math
import re
from collections import Counter
from app.retrieval.lexical import tokenize

PLANNER_VERSION = "clause-queries-v1"
SELECTOR_VERSION = "weighted-term-coverage-v1"
# Generic conversational words, without corpus names or evaluation labels.
STOP = set("我们 我的 这个 那个 应该 怎样 怎么 什么 是否 能否 可以 需要 为什么 多少 多久 现在 同时 并且 另外 然后 请问 处理 规定 要求 公司 不能 直接 出现 连接 笔记 一个 我要 要把 把一 发给 还收 收到 现在 现应 立即 做什 我在 在出 途中 不是 是我 我发 分析 原因 方法 客户端 应对 措施 相关".split())


def terms(text):
    return {t for t in tokenize(text) if t not in STOP and not t.isdigit()}


def plan_queries(query, max_subqueries=3):
    """Retain the exact original; split explicit clauses without inventing facts.

    Deterministic planning, not semantic LLM planning. Carry complete short leading
    context into dependent follow-ups. Long/history queries fall back unchanged.
    """
    if len(query) > 1024 or "前一轮问题：" in query:
        return [query]
    parts = [p.strip() for p in re.split(r"[。！？?！；;\n]+|[，,](?=(?:还|又|同时|并且|另外|为什么|是否|能否|现在))", query) if p.strip()]
    useful = [p for p in parts if len(terms(p)) >= 2]
    if len(useful) < 2:
        return [query]
    result = [query]
    anchor = useful[0]
    for part in useful:
        focused = part
        if part != anchor and re.match(r"^(?:我|它|这|那|为什么|是否|能否|现在|应该)", part) and len(anchor) <= 160:
            focused = anchor + "；" + part
        if focused != query and focused not in result:
            result.append(focused)
        if len(result) == max_subqueries + 1:
            break
    return result


def select_evidence(query, hits, top_k, queries=None, anchor_ids=None):
    """Greedy rank relevance + new query information - redundant content.

    Scores from cosine/BM25/rerank are not mixed. Term rarity uses only the
    authorized pool. Source novelty is a soft bonus for matching evidence,
    never an unrelated-document quota. Preserve full text and citation locations.
    """
    unique, seen = [], set()
    for h in hits:
        # A heading without body text supplies no rule or factual evidence.
        if h["chunk_id"] not in seen and any(line.strip() and not line.lstrip().startswith("#") for line in h["text"].splitlines()):
            unique.append(h)
            seen.add(h["chunk_id"])
    if not unique or top_k < 1:
        return []
    wanted = set().union(*(terms(q) for q in (queries or [query])))
    body = [terms("\n".join(line for line in h["text"].splitlines() if not line.lstrip().startswith("#"))) for h in unique]
    matched = [ts & wanted for ts in body]
    df = Counter(t for ts in matched for t in ts)
    weights = {t: math.log1p(len(unique) / (1 + df[t])) * (2 if re.search(r"[a-z]", t) else 1) for t in wanted if df[t]}
    total = sum(weights.values()) or 1
    covered, documents, selected, remaining = set(), Counter(), [], list(range(len(unique)))
    # Reserve a small core of the exact original ranking. Novel terms must not
    # evict all direct answers when the planner changes wording. The remaining
    # budget is selected by marginal information gain, without source quotas.
    seeds = set((anchor_ids or [h["chunk_id"] for h in hits])[:min(5, (top_k + 1) // 2)])
    for i in list(remaining):
        if unique[i]["chunk_id"] in seeds:
            selected.append(i)
            covered.update(matched[i])
            documents[(unique[i]["document_id"], unique[i]["citation"]["chunk_set_id"])] += 1
            remaining.remove(i)
    while remaining and len(selected) < top_k:
        def utility(i):
            relevance = 1 / (1 + i / 5)
            gain = sum(weights[t] for t in matched[i] - covered) / total
            similarity = max((len(body[i] & body[j]) / max(1, len(body[i] | body[j])) for j in selected), default=0)
            doc = (unique[i]["document_id"], unique[i]["citation"]["chunk_set_id"])
            novelty = .22 / (1 + documents[doc]) if matched[i] else 0
            return .55 * relevance + .5 * gain + novelty - .2 * similarity
        best = max(remaining, key=lambda i: (utility(i), -i))
        selected.append(best)
        covered.update(matched[best])
        documents[(unique[best]["document_id"], unique[best]["citation"]["chunk_set_id"])] += 1
        remaining.remove(best)
    return [unique[i] for i in selected]
