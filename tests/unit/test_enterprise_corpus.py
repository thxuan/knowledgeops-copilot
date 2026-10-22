"""Source extraction and reproducible corpus preparation; no network or model calls."""
import json
import pytest
from scripts.enterprise_corpus import BASE, LICENSE, VERSION, build, extract, sha, verify, local_path

HTML = ('<html lang="zh_CN"><header>网站导航不应入库</header><article>'
        '<div>上一页</div><h1>企业 VPN 连接</h1><p>联系管理员确认连接信息，'
        '在组织批准的软件中设置网络连接，不自行绕过企业的访问管理要求。</p>'
        '<ol><li><p>打开网络设置。</p></li><li><p>填写连接信息。</p></li></ol>'
        '<pre>status\\n  check --verbose</pre>'
        '<section role="navigation">相关推荐不应入库</section></article>'
        '<footer>GNOME 文档项目；中文译者。<a href="' + LICENSE + '">许可</a></footer></html>')


def fixture_corpus(directory):
    content = HTML.encode("utf-8")
    raw = directory / "raw"
    raw.mkdir(parents=True)
    (raw / "net-vpn-connect.html").write_bytes(content)
    (directory / "sources.lock.json").write_text(json.dumps({"profile": VERSION, "sources": [{
        "page": "net-vpn-connect", "url": BASE + "net-vpn-connect.html.zh_CN",
        "sha256": sha(content), "license": "CC-BY-SA-3.0"}]}), encoding="utf-8")
    return build(directory)


def test_extract_retains_steps_and_attribution_without_navigation():
    title, text, credits = extract(HTML)
    assert title == "企业 VPN 连接"
    assert text.startswith("# 企业 VPN 连接")
    assert "- 打开网络设置。" in text and "- 填写连接信息。" in text
    assert "网站导航不应入库" not in text and "相关推荐不应入库" not in text
    assert "上一页" not in text
    assert "GNOME 文档项目" in credits


@pytest.mark.parametrize("html", [
    HTML.replace('lang="zh_CN"', 'lang="en"'),
    HTML.replace(LICENSE, "https://example.invalid/no-license"),
    HTML.replace("<article>", "<aside>").replace("</article>", "</aside>"),
])
def test_reject_non_chinese_or_unlicensed_or_missing_body(html):
    with pytest.raises(ValueError):
        extract(html)


def test_build_preserves_all_policy_sources_versions_and_acl(tmp_path):
    result = fixture_corpus(tmp_path)
    assert result["public_documents"] == 1 and result["local_policy_documents"] == 14
    manifest = verify(tmp_path)
    by_file = {r["filename"]: r for r in manifest["documents"]}
    engineering = by_file["06-engineering-access.md"]
    assert engineering["user_ids"] == ["u_engineer_001"]
    assert engineering["source"] == "sample_data/knowledge_base/06-engineering-access.md"
    assert engineering["version"] == "1.9"
    assert by_file["07-incident-priority-and-escalation.md"]["user_ids"] == ["u_support_001"]
    assert by_file["gnome-net-vpn-connect.md"]["user_ids"] == [
        "u_employee_001", "u_engineer_001", "u_support_001"]
    document = tmp_path / "documents/gnome-net-vpn-connect.md"
    content = document.read_text(encoding="utf-8")
    assert "CC BY-SA 3.0" in content and "Linux/GNOME" in content and "中文译者" in content
    document.write_text(content + "tampered", encoding="utf-8")
    with pytest.raises(ValueError, match="integrity"):
        verify(tmp_path)


def test_reject_manifest_path_escape(tmp_path):
    with pytest.raises(ValueError, match="escapes"):
        local_path(tmp_path, "../outside.md")
