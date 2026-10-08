"""Transport-neutral retrieval and citation objects, independent of database rows."""
from dataclasses import dataclass
from urllib.parse import urlencode, quote


@dataclass(frozen=True)
class Location:
    start_char: int
    end_char: int
    start_line: int
    end_line: int
    coordinate_system: str = "cleaned_body:zero_based_half_open"


@dataclass(frozen=True)
class Citation:
    chunk_id: str
    document_id: str
    chunk_set_id: str
    source: str
    version: str
    title: str
    heading_paths: list[list[str]]
    text_hash: str
    parser_version: str
    location: Location
    url: str
    page: int | None = None


@dataclass(frozen=True)
class RetrievalHit:
    chunk_id: str
    document_id: str
    text: str
    score: float
    source: str
    version: str
    citation: Citation


def make_citation(chunk, set_id):
    return Citation(chunk_id=chunk["chunk_id"], document_id=chunk["document_id"], chunk_set_id=set_id,
        source=chunk["source"], version=chunk["version"], title=chunk["title"], heading_paths=chunk["heading_paths"],
        text_hash=chunk["text_hash"], parser_version=chunk["parser_version"],
        location=Location(**{k: chunk[k] for k in ["start_char", "end_char", "start_line", "end_line"]}),
        url="/citations/" + quote(chunk["chunk_id"], safe="") + "?" + urlencode({"chunk_set_id": set_id}))
