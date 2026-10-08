"""Small-corpus BM25 with a frozen Chinese tokenizer."""
from collections import Counter
import math
import re

TOKENIZER_VERSION = "cjk-bigram-ascii-identifier-v1"


def tokenize(text):
    tokens = re.findall(r"[a-z0-9]+(?:[-_.][a-z0-9]+)*", text.lower())
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        tokens.extend(run[i:i + 2] for i in range(max(1, len(run) - 1)))
    return tokens


def evidence_text(hit):
    citation = hit.get("citation", hit)
    headings = "\n".join(" > ".join(p) for p in citation["heading_paths"])
    return citation["title"] + "\n" + headings + "\n" + hit["text"]


def bm25(query, hits, top_k, k1=1.2, b=0.75):
    """Statistics use only the caller's authorized corpus."""
    if not hits:
        return []
    counts = [Counter(tokenize(evidence_text(h))) for h in hits]
    lengths = [sum(c.values()) for c in counts]
    avg = sum(lengths) / len(lengths) or 1
    frequency = Counter(t for c in counts for t in c)
    terms = set(tokenize(query))
    ranked = []
    for hit, doc_counts, length in zip(hits, counts, lengths, strict=True):
        score = 0.0
        for term in terms:
            tf = doc_counts[term]
            if tf:
                df = frequency[term]
                idf = math.log1p((len(hits) - df + .5) / (df + .5))
                score += idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * length / avg))
        if score > 0:
            ranked.append({**hit, "score": score})
    return sorted(ranked, key=lambda h: (-h["score"], h["chunk_id"]))[:top_k]


def rrf(rankings, rank_constant=60):
    """Combine ranks, never add BM25 and cosine scores directly."""
    if rank_constant < 1:
        raise ValueError("RRF constant must be positive")
    fused = {}
    for name, hits in rankings.items():
        seen = set()
        for rank, hit in enumerate(hits, 1):
            key = hit["chunk_id"]
            if key in seen:
                continue
            seen.add(key)
            if key not in fused:
                fused[key] = {**hit, "score": 0.0, "ranks": {}, "component_scores": {}}
            fused[key]["score"] += 1 / (rank_constant + rank)
            fused[key]["ranks"][name] = rank
            fused[key]["component_scores"][name] = hit["score"]
    return sorted(fused.values(), key=lambda h: (-h["score"], h["chunk_id"]))
