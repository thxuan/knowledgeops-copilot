"""UTF-8 Markdown/TXT parsing; retain structure for Day 4 chunking."""

import re
from dataclasses import dataclass

PARSER_VERSION = "utf8-structure-v1"


class ParseError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ParsedDocument:
    text: str
    title: str
    metadata: dict[str, str]


def parse_document(filename: str, content: bytes) -> ParsedDocument:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ParseError("INVALID_ENCODING", "Document must use UTF-8 encoding") from exc
    if "\x00" in text:
        raise ParseError("BINARY_CONTENT", "Document contains binary content")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    metadata: dict[str, str] = {}
    if filename.lower().endswith(".md") and text.startswith("---\n"):
        lines = text.split("\n")
        try:
            end = lines.index("---", 1)
        except ValueError as exc:
            raise ParseError("INVALID_FRONTMATTER", "Markdown front matter is not closed") from exc
        # A small scalar subset, not general YAML. ACL fields are never trusted.
        for line in lines[1:end]:
            key, separator, value = line.partition(":")
            if separator and key in {"title", "version", "document_id", "effective_date"}:
                metadata[key] = value.strip().strip("\"'")
        text = "\n".join(lines[end + 1:])
    heading = re.search(r"^#\s+(.+)$", text, re.MULTILINE) if filename.lower().endswith(".md") else None
    title = metadata.get("title") or (heading.group(1).strip() if heading else filename)
    return ParsedDocument(text, title, metadata)


def clean_document(parsed: ParsedDocument) -> ParsedDocument:
    # Keep headings, tables, code fences, indentation and Markdown hard breaks.
    text = parsed.text.strip("\n")
    if not text.strip():
        raise ParseError("EMPTY_TEXT", "Document has no usable text")
    return ParsedDocument(text + "\n", parsed.title, parsed.metadata)

