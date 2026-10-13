"""Offline protocol doubles; these are never runtime providers."""
import json, re, hashlib, math
from app.core.embedding import EmbeddingResult
from app.generation.models import GeneratedClaims
from app.generation.provider import terms
from app.agent.provider import tool_message
from app.agent.contracts import dump
from app.retrieval.rerank import RerankResult
from app.retrieval.lexical import bm25
CALC_PATTERN = re.compile(r"^\s*(?:calc\s*:|计算\s*[:：]?)\s*(-?\d+(?:\.\d+)?)\s*([+*/-])\s*(-?\d+(?:\.\d+)?)\s*$", re.I)
class ExtractiveGenerationDouble:
    async def generate(self, messages, settings):
        payload = json.loads(messages[-1]["content"])
        question = payload["question"]
        history = payload["history"] or payload["summary"]
        if any(word in question for word in ("它", "这个", "那个", "上述", "刚才", "多久", "然后")) and history:
            question = " ".join(h["text"] for h in history) + " " + question
        query_terms = terms(question)
        codes = re.findall(r"\b[a-z]+-\d+\b", question.lower())
        candidates = []
        for hit in payload["evidence"]:
            for line in hit["text"].splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if codes and codes[-1] not in hit["text"].lower():
                    continue
                overlap = len(query_terms & terms(line))
                if overlap >= 2:
                    candidates.append((overlap, line, hit["chunk_id"]))
        if not candidates:
            return GeneratedClaims(insufficient_context=True, claims=[]).model_dump_json()
        _, text, cid = max(candidates, key=lambda c: c[0])
        result = GeneratedClaims(insufficient_context=False,
                                 claims=[{"text": text, "citations": [cid]}]).model_dump_json()
        if len(result.encode("utf-8")) > settings.output_limit:
            return GeneratedClaims(insufficient_context=True, claims=[]).model_dump_json()
        return result


class HashEmbeddingDouble:
    def __init__(self, settings):
        self.settings = settings

    async def embed(self, texts):
        vectors = []
        for text in texts:
            tokens = re.findall(r"[a-z0-9]+(?:[-_.][a-z0-9]+)*", text.lower())
            for run in re.findall(r"[\u4e00-\u9fff]+", text):
                tokens.extend(run[i:i + 2] for i in range(max(1, len(run) - 1)))
            vector = [0.0] * self.settings.dimensions
            for token in tokens or ["<empty>"]:
                digest = hashlib.sha256(token.encode()).digest()
                vector[int.from_bytes(digest[:4], "big") % len(vector)] += 1 if digest[4] % 2 else -1
            norm = math.hypot(*vector)
            if norm == 0:
                vector[0], norm = 1.0, 1.0
            vectors.append([x / norm for x in vector])
        return EmbeddingResult(self.settings.model_id, vectors, 0)


class ScriptedAgentDouble:
    """Small deterministic demo; scripted evaluation is separate from model quality."""
    async def complete(self, messages, tools):
        if messages[-1]["role"] == "tool":
            message = {"role": "assistant", "content": dump({"status": "completed",
                       "answer": "工具结果（离线演示）：" + messages[-1]["content"]})}
        else:
            question = messages[-1]["content"]
            match = CALC_PATTERN.fullmatch(question)
            if match:
                message = tool_message("calculator", {"left": float(match[1]), "operator": match[2], "right": float(match[3])})
            elif "创建工单" in question:
                message = tool_message("create_ticket", {"title": "本地模拟支持请求", "description": question[:1000]})
            elif "设备" in question:
                message = tool_message("query_business_data", {"query": "device_status"})
            else:
                message = {"role": "assistant", "content": dump({"status": "needs_clarification",
                    "answer": "请补充系统、具体问题和处理目标。离线演示支持：计算 2 + 3、查询设备、创建工单。"})}
        return {"message": message, "usage": {}, "model": "test-agent"}

class LexicalRerankDouble:
    async def rerank(self, query, documents):
        hits = [{"chunk_id": str(i), "title": "", "heading_paths": [], "text": text}
                for i, text in enumerate(documents)]
        scores = {int(h["chunk_id"]): h["score"] for h in bm25(query, hits, len(hits))}
        return RerankResult([(i, min(1.0, scores.get(i, 0.0) / 10)) for i in range(len(hits))], 0, "test-lexical")
