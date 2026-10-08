from pathlib import Path
import pytest
from app.ingestion.parser import ParseError, clean_document, parse_document


def test_markdown_metadata_and_structure_are_preserved():
    content = ('\ufeff---\r\ntitle: VPN 指南\r\nversion: 2.3\r\n'
               'audience: [admin]\r\n---\r\n\r\n# VPN\r\n'
               '| 错误 | 处理 |\r\n| --- | --- |\r\n| E1 | 重试 |\r\n'
               '~~~python\r\n    x = 1\r\n~~~\r\n段落  \r\n下一行\r\n').encode("utf-8")
    parsed = clean_document(parse_document("vpn.md", content))
    assert parsed.title == "VPN 指南"
    assert parsed.metadata == {"title": "VPN 指南", "version": "2.3"}
    assert parsed.text.startswith("# VPN\n")
    assert "| E1 | 重试 |" in parsed.text
    assert "    x = 1\n" in parsed.text
    assert "段落  \n下一行" in parsed.text
    assert "\r" not in parsed.text


def test_txt_does_not_interpret_frontmatter():
    parsed = clean_document(parse_document("plain.txt", b"---\r\nhello\r\n---"))
    assert parsed.title == "plain.txt"
    assert parsed.text == "---\nhello\n---\n"
    assert parsed.metadata == {}


@pytest.mark.parametrize("content,code", [
    (b"\xff\xfe", "INVALID_ENCODING"), (b"hello\x00", "BINARY_CONTENT"),
    (b"---\ntitle: unclosed\n", "INVALID_FRONTMATTER"), (b" \n\t", "EMPTY_TEXT"),
    (b"---\ntitle: only metadata\n---\n", "EMPTY_TEXT"),
])
def test_invalid_documents_have_specific_errors(content, code):
    with pytest.raises(ParseError) as error:
        clean_document(parse_document("bad.md", content))
    assert error.value.code == code


DOCUMENTS = sorted((Path(__file__).resolve().parents[2] / "sample_data/knowledge_base").glob("*.md"))


@pytest.mark.parametrize("path", DOCUMENTS, ids=lambda path: path.name)
def test_day1_source_documents_parse(path):
    parsed = clean_document(parse_document(path.name, path.read_bytes()))
    assert parsed.metadata["document_id"].startswith("KB-")
    assert parsed.metadata["version"]
    assert parsed.title
    assert parsed.text.startswith("# ")

