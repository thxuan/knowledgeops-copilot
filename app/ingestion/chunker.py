"""Character chunks with exact offsets into the cleaned, versioned body."""

import hashlib
import json
import re
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, replace
from typing import Literal

CHUNKER_VERSION = "char-structure-v2"


@dataclass(frozen=True)
class ChunkConfig:
    strategy: Literal["fixed", "structure"] = "structure"
    size: int = 400
    overlap: int = 0

    def __post_init__(self):
        if self.strategy not in {"fixed", "structure"}:
            raise ValueError("Unknown chunk strategy")
        if type(self.size) is not int or type(self.overlap) is not int:
            raise ValueError("Size and overlap must be integers")
        if not 32 <= self.size <= 8192 or not 0 <= self.overlap < self.size:
            raise ValueError("Require 32 <= size <= 8192 and 0 <= overlap < size")

    @property
    def config_id(self) -> str:
        return f"{CHUNKER_VERSION}:{self.strategy}:{self.size}:{self.overlap}"


@dataclass(frozen=True)
class Section:
    start: int
    end: int
    heading_path: tuple[str, ...]
    level: int
    subtree_end: int


@dataclass(frozen=True)
class Chunk:
    document_id: str
    chunk_id: str
    ordinal: int
    text: str
    title: str
    heading_paths: tuple[tuple[str, ...], ...]
    start_char: int
    end_char: int
    start_line: int
    end_line: int
    source: str
    version: str
    parser_version: str
    text_hash: str
    config_id: str
    split_reason: str
    previous_chunk_id: str | None = None
    next_chunk_id: str | None = None


def text_revision(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _scan_lines(text: str):
    """Recognize ATX headings and blank boundaries outside backtick/tilde fences."""
    offset, fence = 0, None
    for line in text.splitlines(keepends=True):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line.rstrip("\n"))
        inside = fence is not None
        if marker:
            run, suffix = marker.groups()
            if fence is None:
                fence = (run[0], len(run))
            elif run[0] == fence[0] and len(run) >= fence[1] and not suffix.strip():
                fence = None
        heading = None
        if not inside and not marker:
            heading = re.match(r"^ {0,3}(#{1,6})[ \t]+(.+?)[ \t]*\n?$", line)
        yield offset, line, heading, not inside and not marker and not line.strip()
        offset += len(line)


def markdown_sections(text: str, markdown: bool = True) -> list[Section]:
    headings, path = [], []
    if markdown:
        for offset, _, match, _ in _scan_lines(text):
            if match:
                level = len(match.group(1))
                label = re.sub(r"[ \t]+#+[ \t]*$", "", match.group(2)).strip()
                path = [(depth, title) for depth, title in path if depth < level]
                path.append((level, label))
                headings.append((offset, level, tuple(title for _, title in path)))
    if not headings:
        return [Section(0, len(text), (), 0, len(text))]
    sections = []
    if headings[0][0] > 0:
        sections.append(Section(0, headings[0][0], (), 0, headings[0][0]))
    # Next heading of equal/lower depth closes a subtree; compute in linear time.
    subtree_ends, stack = [len(text)] * len(headings), []
    for index in range(len(headings) - 1, -1, -1):
        while stack and headings[stack[-1]][1] > headings[index][1]:
            stack.pop()
        if stack:
            subtree_ends[index] = headings[stack[-1]][0]
        stack.append(index)
    for index, (start, level, heading_path) in enumerate(headings):
        end = headings[index + 1][0] if index + 1 < len(headings) else len(text)
        subtree_end = subtree_ends[index]
        sections.append(Section(start, end, heading_path, level, subtree_end))
    return sections


def chunk_document(document_id: str, text: str, *, title: str, source: str,
                   version: str, parser_version: str, config: ChunkConfig,
                   markdown: bool = True, generation: int = 1) -> list[Chunk]:
    if not text.strip():
        raise ValueError("Cannot chunk empty text")
    sections = markdown_sections(text, markdown)
    section_starts = [s.start for s in sections]
    revision = text_revision(text)
    line_starts = [0] + [i + 1 for i, char in enumerate(text) if char == "\n"]
    ranges = []
    scopes = [(0, len(text))] if config.strategy == "fixed" else [(s.start, s.end) for s in sections]
    boundaries = [offset + len(line) for offset, line, _, blank in _scan_lines(text) if blank]
    for scope_start, scope_end in scopes:
        start = scope_start
        while start < scope_end:
            limit = min(start + config.size, scope_end)
            end, reason = limit, "fixed"
            if config.strategy == "structure":
                candidate_index = bisect_right(boundaries, limit) - 1
                candidate = boundaries[candidate_index] if candidate_index >= 0 else start
                if limit == scope_end:
                    reason = "boundary"
                elif candidate > start + config.overlap:
                    end, reason = candidate, "boundary"
                else:
                    reason = "hard_limit"
            ranges.append((start, end, reason))
            if end == scope_end:
                break
            start = end - config.overlap
    chunks = []
    for index, (start, end, reason) in enumerate(ranges):
        first = max(0, bisect_right(section_starts, start) - 1)
        stop = bisect_left(section_starts, end)
        paths = tuple(dict.fromkeys(s.heading_path for s in sections[first:stop]))
        key = json.dumps([document_id, revision, parser_version, config.config_id, source, version, title, generation, start, end])
        chunks.append(Chunk(
            document_id=document_id, chunk_id=hashlib.sha256(key.encode()).hexdigest(), ordinal=index,
            text=text[start:end], title=title, heading_paths=paths, start_char=start, end_char=end,
            start_line=bisect_right(line_starts, start), end_line=bisect_right(line_starts, end - 1),
            source=source, version=version, parser_version=parser_version, text_hash=revision,
            config_id=config.config_id, split_reason=reason,
        ))
    return [replace(c, previous_chunk_id=chunks[i - 1].chunk_id if i else None,
                    next_chunk_id=chunks[i + 1].chunk_id if i + 1 < len(chunks) else None)
            for i, c in enumerate(chunks)]
