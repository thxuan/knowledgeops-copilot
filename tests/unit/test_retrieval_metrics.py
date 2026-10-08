from app.evals.retrieval import measure
import pytest


def test_source_recall_requires_version_and_evidence_position():
    question = {"question_id":"q","answerable":True,"expected_sources":[{"document_id":"A","version":"1"},{"document_id":"B","version":"1"}]}
    anchors = [{"question_id":"q","document_id":d,"version":"1","text_hash":"hash","parser_version":"p","start_char":10,"end_char":20} for d in ["A","B"]]
    def hit(source,start,end,version="1"):
        return {"source":source,"citation":{"version":version,"text_hash":"hash","parser_version":"p","location":{"start_char":start,"end_char":end}}}
    ids={"a":"A","b":"B"}
    assert measure(question,[hit("a",0,10),hit("b",10,20,"old")],anchors,ids)["source_recall_at_k"] == 0
    metrics=measure(question,[hit("a",10,15),hit("a",12,15)],anchors,ids)
    assert metrics["source_recall_at_k"] == .5 and metrics["section_char_coverage_at_k"] == .25
    bad=hit("a",10,20)
    bad["citation"]["text_hash"]="changed"
    with pytest.raises(ValueError):
        measure(question,[bad],anchors,ids)


def test_unanswerable_has_no_recall_denominator():
    assert measure({"question_id":"q","answerable":False,"expected_sources":[]},[],[],{})["source_recall_at_k"] is None
