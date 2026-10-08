"""Run the Day 6 service against the frozen dev split, without gold-assisted ranking."""
import argparse
import asyncio
from dataclasses import replace
from datetime import datetime, timezone, timedelta
import hashlib
import json
import logging
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.core.config import Settings
from app.core.embedding import make_embedding_client
from app.core.identity import LOCAL_IDENTITIES
from app.db.ingestion import IngestionRepository
from app.db.snapshots import database_label
from app.db.chunks import ChunkRepository
from app.db.vectors import VectorRepository
from app.evals.retrieval import measure
from app.retrieval.service import VectorService
from scripts.compare_chunks import load_corpus


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--report-name", default="day6-recall")
    args = parser.parse_args()
    if Path(args.report_name).name != args.report_name:
        raise SystemExit("Report name must be a filename stem")
    settings = Settings.from_env()
    if not settings.vector_database_url:
        raise SystemExit("Set KNOWLEDGEOPS_DATABASE_URL first")
    intake = IngestionRepository(args.database_url or settings.database_url)
    intake.initialize()
    chunks = ChunkRepository(intake)
    chunks.initialize()
    vectors = VectorRepository(args.database_url or settings.database_url, settings.embedding)
    vectors.initialize()
    service = VectorService(chunks,vectors,make_embedding_client(settings.embedding),settings.embedding)
    corpus = load_corpus()
    source_ids = {"sample_data/knowledge_base/" + doc["filename"]: doc_id for doc_id,doc in corpus.items()}
    questions_path = ROOT / "eval_data/questions.jsonl"
    anchors_path = ROOT / "eval_data/day4-source-spans.jsonl"
    questions = [json.loads(line) for line in questions_path.read_text(encoding="utf-8").splitlines()]
    questions = [q for q in questions if q["split"] == "dev"]
    anchors = [json.loads(line) for line in anchors_path.read_text(encoding="utf-8").splitlines()]
    with intake.connection() as db:
        documents = [dict(row) for row in db.execute("""SELECT d.document_id,d.source,d.version,d.text,d.parser_version,h.chunk_set_id
            FROM documents d JOIN document_chunk_heads h USING(document_id) WHERE d.active=1""")]
    if {d["source"] for d in documents} != set(source_ids) or len(documents) != len(corpus):
        raise SystemExit("Use a complete isolated 14-document sample index")
    for document in documents:
        doc = corpus[source_ids[document["source"]]]
        if any(document[key] != doc[key] for key in ["version","text","parser_version"]):
            raise SystemExit("Corpus differs from the frozen fixture")
        snapshot = chunks.get(document["document_id"], LOCAL_IDENTITIES[doc["allowed_user_ids"][0]])
        if not vectors.ready(document["document_id"],document["chunk_set_id"],snapshot["count"]):
            raise SystemExit("An active sample document has no complete vector index")
    ks = [1,3,5,10]
    details = []
    for question in questions:
        identity = LOCAL_IDENTITIES[question["allowed_user_ids"][0]]
        result = await service.retrieve(question["question"],identity,top_k=max(ks))
        details.append({"question_id":question["question_id"],"question_type":question["question_type"],
            "answerable":question["answerable"],"user_id":identity.user_id,
            "duration_ms_at_10":result["duration_ms"],"metrics":{
                str(k):measure(question,result["hits"][:k],anchors,source_ids) for k in ks},
            "hits":result["hits"]})
    answerable = [d for d in details if d["answerable"]]
    summary = [{"top_k":k,"mean_source_recall_at_k":sum(d["metrics"][str(k)]["source_recall_at_k"] for d in answerable)/len(answerable),
        "mean_section_char_coverage_at_k":sum(d["metrics"][str(k)]["section_char_coverage_at_k"] for d in answerable)/len(answerable),
        "unanswerable_with_hits":sum(d["metrics"][str(k)]["retrieved_count"]>0 for d in details if not d["answerable"])} for k in ks]
    # One restrictive filter, chosen independently of questions/gold, with actual query plans.
    vectors.create_hnsw()
    identity = LOCAL_IDENTITIES["u_employee_001"]
    first = sorted(service.allowed_sets(identity),key=lambda d:d["document_id"])[0]
    allowed = service.allowed_sets(identity,{"document_ids":[first["document_id"]]})
    query_vector = (await service.client.embed(["VPN"])).vectors[0]
    plans = {mode:vectors.search(query_vector,allowed,identity.tenant_id,5,mode,explain=True) for mode in ["exact","hnsw"]}
    report = {"run_date":datetime.now(timezone(timedelta(hours=8))).date().isoformat(),
        "provider":settings.embedding.provider,"model_id":settings.embedding.model_id,"dimensions":settings.embedding.dimensions,
        "collection":settings.embedding.collection,"mode":"exact","chunk_config":"structure/400/0 characters",
        "split":"dev","question_count":len(details),"answerable_count":len(answerable),
        "annotation":"A source hit requires the expected version and intersection with a frozen section span; it is not full answer evidence coverage.",
        "scope":"Assistant experiment; fake embeddings; no generation, refusal, held-out test or real provider evaluation.",
        "database":database_label(args.database_url or settings.database_url),"summary":summary,"details":details,"filtered_query_plans":plans,
        "questions_sha256":hashlib.sha256(questions_path.read_bytes()).hexdigest(),
        "anchors_sha256":hashlib.sha256(anchors_path.read_bytes()).hexdigest(),
        "corpus":[{k:d[k] for k in ["document_id","source","version","chunk_set_id"]} for d in documents],
        "code_sha256":{name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in
            ["app/retrieval/service.py","app/retrieval/objects.py","app/db/vectors.py","app/ingestion/chunker.py","app/evals/retrieval.py","scripts/evaluate_retrieval.py"]}}
    directory = ROOT / "docs/experiments"
    (directory/(args.report_name+".json")).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({"question_count":len(details),"answerable_count":len(answerable),"summary":summary},indent=2))


if __name__ == "__main__":
    logging.getLogger("knowledgeops").setLevel(logging.WARNING)
    asyncio.run(main())
