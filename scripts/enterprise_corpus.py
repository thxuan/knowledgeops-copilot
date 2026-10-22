"""Build a licensed Chinese enterprise-support corpus and import into an isolated schema."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
from html import unescape
import json
import os
from pathlib import Path
import re
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
PROFILE = ROOT / "datasets/enterprise-zh"
VERSION = "enterprise-zh-2026-10-16-v1"
LICENSE = "https://creativecommons.org/licenses/by-sa/3.0/"
BASE = "https://help.gnome.org/gnome-help/"
PUBLIC_USERS = ["u_employee_001", "u_engineer_001", "u_support_001"]


def sha(content):
    return hashlib.sha256(content).hexdigest()


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def local_path(root, relative):
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("Path escapes corpus directory")
    return path


class Article(HTMLParser):
    """Extract article text only: exclude site navigation, SVG and related-page lists."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.parts = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        active = tag == "article" or bool(self.stack and self.stack[-1][1])
        skip = bool(self.stack and self.stack[-1][2]) or tag in {"script", "style", "svg"}
        skip = skip or attrs.get("role") == "navigation"
        if active and not skip:
            if re.fullmatch(r"h[1-6]", tag):
                self.parts.append("\n\n" + "#" * int(tag[1]) + " ")
            elif tag == "pre":
                self.parts.append("\n\n```\n")
            elif tag in {"p", "div", "section", "table", "tr", "blockquote"}:
                self.parts.append("\n\n")
            elif tag == "li":
                self.parts.append("\n- ")
            elif tag == "br":
                self.parts.append("\n")
            elif tag == "img":
                self.parts.append(attrs.get("alt", ""))
            elif tag in {"td", "th"}:
                self.parts.append(" | ")
        if tag not in {"br", "img", "hr", "meta", "link", "input", "source", "wbr"}:
            self.stack.append((tag, active, skip))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                active, skip = self.stack[index][1:]
                self.stack = self.stack[:index]
                if active and not skip and tag == "pre":
                    self.parts.append("\n```\n\n")
                elif active and not skip and tag in {"p", "li", "div", "section", "tr", "blockquote"}:
                    self.parts.append("\n\n")
                break

    def handle_data(self, data):
        if self.stack and self.stack[-1][1] and not self.stack[-1][2]:
            self.parts.append(data if any(frame[0] == "pre" for frame in self.stack) else re.sub(r"\s+", " ", data))

    def text(self):
        text = "".join(self.parts)
        segments = re.split(r"(```\n.*?\n```)", text, flags=re.S)
        for index in range(0, len(segments), 2):
            segment = re.sub(r"[ \t]+\n", "\n", segments[index])
            segment = re.sub(r"\n[ \t]+", "\n", segment)
            segment = re.sub(r"\n-\s*\n+", "\n- ", segment)
            segments[index] = re.sub(r"\n{3,}", "\n\n", segment)
        return "".join(segments).strip() + "\n"


def extract(html):
    if not re.search(r'lang=["\']zh_CN["\']', html):
        raise ValueError("Source is not explicitly Simplified Chinese")
    if LICENSE not in html:
        raise ValueError("Unexpected or missing source license")
    parser = Article()
    parser.feed(html)
    text = parser.text()
    heading = re.search(r"(?m)^# ", text)
    text = text[heading.start():] if heading else text
    if not text.startswith("# ") or len(re.findall(r"[\u4e00-\u9fff]", text)) < 30:
        raise ValueError("Missing Chinese article body")
    footer = re.search(r"<footer>(.*?)</footer>", html, re.S)
    credits = re.sub(r"<[^>]*>", " ", footer.group(1)) if footer else ""
    credits = unescape(re.sub(r"\s+", " ", credits).strip())
    if not credits:
        raise ValueError("Missing attribution")
    return text.splitlines()[0][2:], text, credits


def fetch(directory):
    config = json.loads((PROFILE / "sources.json").read_text(encoding="utf-8"))
    pinned_file = PROFILE / "sources.lock.json"
    pinned = json.loads(pinned_file.read_text(encoding="utf-8")) if pinned_file.exists() else None
    expected = {s["page"]: s["sha256"] for s in pinned["sources"]} if pinned else {}
    if pinned and set(expected) != set(config["pages"]):
        raise ValueError("Source selection differs from lock")
    def one(page):
        if not re.fullmatch(r"[a-z0-9-]+", page):
            raise ValueError("Invalid source page")
        url = BASE + page + ".html.zh_CN"
        path = directory / "raw" / (page + ".html")
        if path.exists():
            content = path.read_bytes()
        else:
            for attempt in range(3):
                try:
                    with urllib.request.urlopen(url, timeout=30) as response:
                        if not response.url.startswith(BASE):
                            raise ValueError("Source redirected outside approved help directory")
                        content = response.read(1_000_001)
                    break
                except OSError:
                    if attempt == 2:
                        raise
                    time.sleep(attempt + 1)
            if len(content) > 1_000_000:
                raise ValueError("Source page too large")
        title, _, _ = extract(content.decode("utf-8"))
        if expected and sha(content) != expected[page]:
            raise ValueError("Source changed since pinned snapshot: " + page)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return {"page": page, "title": title, "url": url, "sha256": sha(content),
                "license": "CC-BY-SA-3.0", "license_url": LICENSE}
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [(page, pool.submit(one, page)) for page in config["pages"]]
        sources, errors = [], []
        for page, future in futures:
            try:
                sources.append(future.result())
            except Exception as exc:
                errors.append(page + ": " + str(exc))
        if errors:
            raise ValueError("Source validation failed:\n" + "\n".join(errors))
    snapshot = {"profile": VERSION, "fetched_at": datetime.now(timezone.utc).isoformat(),
                "sources": sources}
    dump(directory / "sources.lock.json", snapshot)
    return snapshot


def build(directory):
    from app.ingestion.parser import clean_document, parse_document, PARSER_VERSION
    from app.ingestion.chunker import ChunkConfig, chunk_document
    from scripts.compare_chunks import load_corpus
    snapshot = json.loads((directory / "sources.lock.json").read_text(encoding="utf-8"))
    records = []
    def save(filename, content, source, version, users, kind):
        parsed = clean_document(parse_document(filename, content))
        chunks = chunk_document(filename, parsed.text, title=parsed.title, source=source, version=version,
                                parser_version=PARSER_VERSION + ":md", config=ChunkConfig(size=800))
        target = directory / "documents" / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        records.append({"filename": filename, "title": parsed.title, "source": source, "version": version,
                        "sha256": sha(content), "text_hash": sha(parsed.text.encode("utf-8")),
                        "parser_version": PARSER_VERSION + ":md", "user_ids": users,
                        "kind": kind, "chunk_count": len(chunks)})
    for source in snapshot["sources"]:
        content = (directory / "raw" / (source["page"] + ".html")).read_bytes()
        if sha(content) != source["sha256"]:
            raise ValueError("Raw source checksum mismatch")
        title, text, credits = extract(content.decode("utf-8"))
        text = (text.splitlines()[0] + "\n\n适用范围：Linux/GNOME 办公终端。界面与发行版可能不同；"
                "企业授权、软件安装、备份位置及网络配置须遵守组织策略，本指南不授予操作权限。\n\n"
                + "\n".join(text.splitlines()[1:]).strip()
                + "\n\n## 来源与许可\n\n" + source["url"] + "\n\n许可：CC BY-SA 3.0。\n\n"
                "处理说明：从官方简体中文 HTML 提取正文，移除导航和图片资源，保留图片文字说明；"
                "加入适用范围和来源标注。改编后的本篇文档按 CC BY-SA 3.0 提供。\n\n"
                "原始署名信息：" + credits + "\n")
        wrapped = ("---\ntitle: " + title + "\nversion: " + VERSION
                   + "\ndocument_id: GNOME-" + source["page"] + "\n---\n" + text).encode("utf-8")
        save("gnome-" + source["page"] + ".md", wrapped, source["url"], VERSION, PUBLIC_USERS, "public-guide")
    # Preserve all reviewed organization rules and their exact source/version/ACL mappings.
    # This keeps existing QA gold, TL-403 tasks and restricted support procedures valid.
    for document in load_corpus().values():
        filename = document["filename"]
        save(filename, (ROOT / "sample_data/knowledge_base" / filename).read_bytes(),
             "sample_data/knowledge_base/" + filename, document["version"], document["allowed_user_ids"], "local-policy")
    manifest = {"profile": VERSION, "public_documents": len(snapshot["sources"]),
                "local_policy_documents": len(records) - len(snapshot["sources"]),
                "document_count": len(records), "chunks_at_800": sum(r["chunk_count"] for r in records),
                "documents": records}
    dump(directory / "manifest.json", manifest)
    (directory / "NOTICE.md").write_text(
        "# 企业技术支持中文知识库\n\n真实技术资料来自 GNOME Documentation Project 及各页所列作者、译者，"
        "按 CC BY-SA 3.0 使用。各篇保留原始出处、署名和改编说明。\n\n" + LICENSE
        + "\n\n本地企业政策来自本项目原有虚构样例，与外部指南分别标注。"
        "公司、业务状态及工单均为模拟，不代表 GNOME 的企业政策或真实客户数据。\n",
        encoding="utf-8")
    return {k: v for k, v in manifest.items() if k != "documents"}


def verify(directory):
    from app.core.identity import LOCAL_IDENTITIES
    from app.ingestion.parser import clean_document, parse_document
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    if manifest["profile"] != VERSION or not manifest["documents"]:
        raise ValueError("Unexpected corpus profile")
    if len({r["source"] for r in manifest["documents"]}) != len(manifest["documents"]):
        raise ValueError("Duplicate source identities")
    for row in manifest["documents"]:
        content = local_path(directory / "documents", row["filename"]).read_bytes()
        parsed = clean_document(parse_document(row["filename"], content))
        if sha(content) != row["sha256"] or sha(parsed.text.encode()) != row["text_hash"]:
            raise ValueError("Document integrity failure: " + row["filename"])
        if not row["user_ids"] or not set(row["user_ids"]) <= LOCAL_IDENTITIES.keys():
            raise ValueError("Unknown local principal")
    return manifest


def import_corpus(directory, database_url, schema):
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo
    from app.core.identity import LOCAL_IDENTITIES
    from app.core.embedding import EmbeddingSettings
    from app.db.ingestion import IngestionRepository
    from app.db.chunks import ChunkRepository
    from app.db.conversations import ConversationRepository
    from app.db.documents import DocumentRepository
    from app.db.vectors import VectorRepository
    from app.ingestion.worker import IngestionWorker
    from app.ingestion.chunker import ChunkConfig
    from scripts.setup_agent_demo import setup
    manifest = verify(directory)
    if not re.fullmatch(r"ko_enterprise_[a-z0-9_]{1,45}", schema):
        raise ValueError("Use an isolated ko_enterprise_ schema")
    with psycopg.connect(database_url, connect_timeout=5) as db:
        db.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
    scoped = make_conninfo(database_url, options=f"-c search_path={schema},public")
    intake = IngestionRepository(scoped)
    intake.initialize()
    chunks = ChunkRepository(intake)
    chunks.initialize()
    ConversationRepository(intake).initialize()
    with intake.connection() as db:
        existing = db.execute("SELECT source,version FROM documents").fetchall()
    selected = {(r["source"], r["version"]) for r in manifest["documents"]}
    if any((r["source"], r["version"]) not in selected for r in existing):
        raise ValueError("Schema already contains another corpus/version")
    VectorRepository(scoped, EmbeddingSettings(collection="enterprise-pending-embedding")).initialize()
    setup(scoped)  # Existing revoked permissions and business values are preserved.
    document_repo = DocumentRepository(intake)
    worker = IngestionWorker(intake)
    result = []
    for row in manifest["documents"]:
        owner = LOCAL_IDENTITIES["u_engineer_001" if row["kind"] == "public-guide" else row["user_ids"][0]]
        content = local_path(directory / "documents", row["filename"]).read_bytes()
        receipt = intake.submit(identity=owner, filename=row["filename"], content=content,
            file_hash=row["sha256"], parser_version=row["parser_version"], source=row["source"], version=row["version"])
        # Imported files remain private drafts; manifest user_ids are proposed publication scope.
        while True:
            task = intake.get_task(receipt["task_id"], owner)
            if task["status"] == "succeeded":
                break
            if task["status"] == "failed" or not worker.process_next():
                raise RuntimeError("Inspect ingestion task: " + receipt["task_id"])
        built = chunks.build(receipt["document_id"], owner, ChunkConfig(size=800), publish=False)
        result.append({"document_id": receipt["document_id"], "source": row["source"], "chunks": built["count"],
                       "duplicate": receipt["duplicate"]})
    report = {"profile": VERSION, "schema": schema, "document_count": len(result),
              "chunks": sum(r["chunks"] for r in result), "embedding_performed": False, "publication_requires_review": True, "documents": result}
    dump(directory / "import-report.json", report)
    return {k: v for k, v in report.items() if k != "documents"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("fetch", "build", "verify", "import"))
    parser.add_argument("--directory", type=Path, default=ROOT / "data" / VERSION)
    parser.add_argument("--database-url", default=os.getenv("KNOWLEDGEOPS_DATABASE_URL"))
    parser.add_argument("--schema", default="ko_enterprise_zh")
    args = parser.parse_args()
    if args.action == "fetch":
        result = fetch(args.directory)
        result = {"profile": VERSION, "downloaded": len(result["sources"])}
    elif args.action == "build":
        result = build(args.directory)
    elif args.action == "verify":
        result = {"verified": True, "documents": verify(args.directory)["document_count"]}
    else:
        from app.db.postgres import DEFAULT_DATABASE_URL
        result = import_corpus(args.directory, args.database_url or DEFAULT_DATABASE_URL, args.schema)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
