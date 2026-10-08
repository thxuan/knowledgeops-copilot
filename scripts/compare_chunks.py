"""Compare five frozen chunk configurations using only the Day 1 dev split."""

import argparse
from datetime import datetime, timezone, timedelta
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.evals.chunk_comparison import compare, resolve_anchors
from app.ingestion.chunker import ChunkConfig
from app.ingestion.parser import PARSER_VERSION, clean_document, parse_document

CONFIGS = [ChunkConfig("fixed", 2000, 0), ChunkConfig("fixed", 200, 0),
           ChunkConfig("fixed", 200, 40), ChunkConfig("structure", 400, 0),
           ChunkConfig("structure", 400, 60)]


def load_corpus():
    corpus = {}
    for file in sorted((ROOT / "sample_data/knowledge_base").glob("*.md")):
        parsed = clean_document(parse_document(file.name, file.read_bytes()))
        doc_id = parsed.metadata["document_id"]
        # Trusted, local fixture policy. Never derive application authorization from front matter.
        users = ["u_employee_001", "u_engineer_001", "u_support_001"]
        if doc_id == "KB-ENG-001":
            users = ["u_engineer_001"]
        elif doc_id == "KB-OPS-001":
            users = ["u_support_001"]
        corpus[doc_id] = {"text": parsed.text, "title": parsed.title, "filename": file.name,
                          "version": parsed.metadata["version"], "parser_version": PARSER_VERSION + ":md",
                          "allowed_user_ids": users}
    return corpus


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--freeze-source-spans", action="store_true")
    args = parser.parse_args()
    corpus = load_corpus()
    questions_path = ROOT / "eval_data/questions.jsonl"
    questions = [json.loads(line) for line in questions_path.read_text(encoding="utf-8").splitlines()]
    anchors_path = ROOT / "eval_data/day4-source-spans.jsonl"
    if args.freeze_source_spans:
        if anchors_path.exists():
            raise SystemExit("Frozen source spans already exist; review corpus changes before replacing them")
        anchors_path.write_text("".join(json.dumps(a, ensure_ascii=False) + "\n"
                                       for a in resolve_anchors(corpus, questions)), encoding="utf-8")
    anchors = [json.loads(line) for line in anchors_path.read_text(encoding="utf-8").splitlines()]
    results = [compare(corpus, questions, anchors, config) for config in CONFIGS]
    report = {"run_date": datetime.now(timezone(timedelta(hours=8))).date().isoformat(), "ranking": "Latin identifiers + Chinese bigram set; IDF/sqrt(unique terms)",
              "annotation": "Day 1 section spans; source hit requires coordinate intersection, not document ID alone",
              "scope": "Offline trusted fixtures; dev only. No embeddings, generation, refusal or latency evaluation.",
               "code_sha256": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                              for name in ["app/ingestion/chunker.py", "app/evals/chunk_comparison.py",
                                           "scripts/compare_chunks.py"]},
              "corpus": [{"document_id": key, "version": doc["version"],
                          "text_hash": hashlib.sha256(doc["text"].encode()).hexdigest(),
                          "allowed_user_ids": doc["allowed_user_ids"]} for key, doc in sorted(corpus.items())],
              "questions_sha256": hashlib.sha256(questions_path.read_bytes()).hexdigest(),
              "anchors_sha256": hashlib.sha256(anchors_path.read_bytes()).hexdigest(), "results": results}
    directory = ROOT / "docs/experiments"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "day4-chunks.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    rows = ["# Day 4 Chunk 参数实验（助手运行）", "",
            f"日期：{report['run_date']}。14 份模拟文档；开发集 20 题，其中 17 题可回答、3 题无答案；Top K=5。保留测试集没有运行。",
            "", "来源位置绑定清洗正文、解析版本与正文哈希；标注为 Day 1 的章节范围，并非逐句答案证据。",
            "来源命中要求检索区间与标注章节相交；章节字符覆盖率衡量取回多少标注内容，不代表答案正确率。",
            "非标注上下文比例只表示标注外文本，不应直接解释为所有文本都无关。", "",
            "| 参数（字符） | Chunk 数 | 存储倍数 | 来源 Recall@5 | 章节字符覆盖@5 | 平均上下文字符 | 非标注比例 |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for r in results:
        rows.append(f"| {r['config_id'].split(':', 1)[1]} | {r['chunk_count']} | {r['storage_expansion']:.3f} | "
                    f"{r['mean_source_recall_at_k']:.3f} | {r['mean_section_char_coverage_at_k']:.3f} | "
                    f"{r['mean_context_chars']:.1f} | {r['mean_nonannotated_context_fraction']:.3f} |")
    rows += ["", "全部配置对 3 条无答案问题均可能检出关键词片段；该实验没有拒答能力，不能据此报告拒答准确率。",
             "排序统一使用正文、文档标题和标题路径；不使用 expected_sources、答案要点或标注内容生成查询／排序特征。",
             "同 size 的 fixed 200/0 与 200/40，以及 structure 400/0 与 400/60 用于观察 overlap，其他组只比较整体取舍。",
             "", "原始逐题结果（含 Top 5 的正文与位置）：[day4-chunks.json](day4-chunks.json)。",
             "冻结来源标注：[day4-source-spans.jsonl](../../eval_data/day4-source-spans.jsonl)。",
             "这是一轮离线关键词诊断；尚未评估 Embedding、完整 RAG 或真实用户分布。"]
    (directory / "day4-chunks.md").write_text("\n".join(rows) + "\n", encoding="utf-8")
    print(json.dumps([{k: v for k, v in r.items() if k != "details"} for r in results], indent=2))


if __name__ == "__main__":
    main()
