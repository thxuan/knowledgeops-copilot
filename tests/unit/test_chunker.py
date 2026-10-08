from pathlib import Path

import pytest

from app.ingestion.chunker import ChunkConfig, chunk_document, markdown_sections
from app.ingestion.parser import clean_document, parse_document

ROOT = Path(__file__).resolve().parents[2]


def make_chunks(text, config=ChunkConfig(), markdown=True):
    return chunk_document("doc-1", text, title="VPN", source="local/vpn",
                          version="1", parser_version="test:md", config=config, markdown=markdown)


@pytest.mark.parametrize("strategy", ["fixed", "structure"])
@pytest.mark.parametrize("size,overlap", [(32, 0), (32, 31), (200, 40), (400, 60)])
def test_all_fixture_characters_are_preserved_with_exact_positions(strategy, size, overlap):
    for file in (ROOT / "sample_data/knowledge_base").glob("*.md"):
        text = clean_document(parse_document(file.name, file.read_bytes())).text
        chunks = make_chunks(text, ChunkConfig(strategy, size, overlap))
        covered = bytearray(len(text))
        for i, c in enumerate(chunks):
            assert c.text == text[c.start_char:c.end_char]
            assert 0 < len(c.text) <= size
            assert c.start_line == text.count("\n", 0, c.start_char) + 1
            assert c.end_line == text.count("\n", 0, c.end_char - 1) + 1
            covered[c.start_char:c.end_char] = b"1" * len(c.text)
            assert c.previous_chunk_id == (chunks[i - 1].chunk_id if i else None)
            assert c.next_chunk_id == (chunks[i + 1].chunk_id if i + 1 < len(chunks) else None)
        assert all(covered)


def test_metadata_retains_hierarchy_and_fixed_chunk_can_cross_headings():
    text = "# VPN\n\n## Errors\n\n### TL-403\n不合规；不得关闭 EDR。\n\n### TL-429\n等待 60 秒。\n"
    structured = make_chunks(text)
    target = next(c for c in structured if "不合规" in c.text)
    assert target.heading_paths == (("VPN", "Errors", "TL-403"),)
    fixed = make_chunks(text, ChunkConfig("fixed", 400, 0))
    assert len(fixed[0].heading_paths) == 4


def test_fenced_fake_heading_and_blank_lines_do_not_split_small_code_or_table():
    text = "# Demo\n\n说明。\n\n```python\n# not a heading\n\nprint(1)\n```\n\n|id|rule|\n|--|--|\n|TL-403|禁止绕过|\n"
    assert len(markdown_sections(text)) == 1
    chunks = make_chunks(text, ChunkConfig("structure", 80, 0))
    assert any("```python\n# not a heading\n\nprint(1)\n```" in c.text for c in chunks)
    assert any("|id|rule|\n|--|--|\n|TL-403|禁止绕过|" in c.text for c in chunks)


def test_oversized_block_records_hard_limit_and_txt_does_not_parse_headings():
    text = "# Title\n" + "x" * 300
    chunks = make_chunks(text, ChunkConfig("structure", 64, 8))
    assert any(c.split_reason == "hard_limit" for c in chunks)
    assert all(c.heading_paths == ((),) for c in make_chunks(text, markdown=False))


def test_deterministic_ids_change_with_text_or_configuration():
    text = "# VPN\n" + "禁止绕过。" * 30
    a = make_chunks(text)
    assert a == make_chunks(text)
    assert {c.chunk_id for c in a}.isdisjoint(c.chunk_id for c in make_chunks(text + "新版本"))
    assert {c.chunk_id for c in a}.isdisjoint(c.chunk_id for c in make_chunks(text, ChunkConfig("fixed", 400, 60)))


@pytest.mark.parametrize("args", [("bad", 100, 0), ("fixed", 0, 0), ("fixed", 32, 32),
                                 ("fixed", 32, -1), ("fixed", 32.5, 0), ("fixed", True, 0)])
def test_invalid_chunk_configuration(args):
    with pytest.raises(ValueError):
        ChunkConfig(*args)
