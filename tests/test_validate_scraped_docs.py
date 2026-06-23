"""Tests for scripts/maint/validate_scraped_docs.py — scraped-doc integrity checks."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

_MOD_PATH = Path(__file__).resolve().parent.parent / "scripts" / "maint" / "validate_scraped_docs.py"
_spec = importlib.util.spec_from_file_location("validate_scraped_docs", _MOD_PATH)
vsd = importlib.util.module_from_spec(_spec)
sys.modules["validate_scraped_docs"] = vsd  # required so @dataclass can resolve __module__
_spec.loader.exec_module(vsd)


# --------------------------- pure helpers ---------------------------------- #
def test_strip_frontmatter():
    assert vsd.strip_frontmatter("---\na: 1\n---\nbody").strip() == "body"
    assert vsd.strip_frontmatter("no frontmatter").strip() == "no frontmatter"


def test_visible_text_len_counts_real_text():
    assert vsd.visible_text_len("# Title\n\nReal content here.") > 10
    # near-empty: only markup/whitespace
    assert vsd.visible_text_len("---\nx: 1\n---\n\n#  \n\n") < 5


def test_find_stub_markers():
    assert "page not found" in vsd.find_stub_markers("Oops — Page Not Found")
    assert "you need to enable javascript" in vsd.find_stub_markers("You need to enable JavaScript to run this app.")
    assert vsd.find_stub_markers("normal docs about payments") == []


def test_unclosed_code_fence():
    assert vsd.has_unclosed_code_fence("text\n```\ncode\n") is True
    assert vsd.has_unclosed_code_fence("text\n```\ncode\n```\n") is False


def test_trailing_partial_table():
    truncated = "| Error | Desc |\n| --- | --- |\n| INSTRUMENT_DECLINED | declined |\n| FOO |"
    assert vsd.trailing_partial_table(truncated) is True
    complete = "| Error | Desc |\n| --- | --- |\n| FOO | bar |\n\nMore prose after the table."
    assert vsd.trailing_partial_table(complete) is False


def test_looks_truncated():
    assert vsd.looks_truncated("intro\n```\nunterminated") is not None
    assert vsd.looks_truncated("All done. Final sentence here.") is None


# --------------------------- crawl summary --------------------------------- #
def test_check_crawl_summary_flags_failures():
    codes = {i.code for i in vsd.check_crawl_summary({"discovered": 10, "extracted": 7, "failed": 3, "errors": ["boom"]}, "p")}
    assert "crawl_failed" in codes
    assert "crawl_errors" in codes
    assert "crawl_incomplete" in codes


def test_check_crawl_summary_clean():
    assert vsd.check_crawl_summary({"discovered": 9, "extracted": 9, "failed": 0, "errors": []}, "p") == []


def test_check_crawl_summary_zero_extracted():
    codes = {i.code for i in vsd.check_crawl_summary({"discovered": 5, "extracted": 0, "failed": 0}, "p")}
    assert "crawl_extracted_zero" in codes


# --------------------------- file-level ------------------------------------ #
def test_check_file_detects_stub(tmp_path):
    f = tmp_path / "x.md"
    f.write_text("# 404\n\nThis page could not be found.\n", encoding="utf-8")
    codes = {i.code for i in vsd.check_file(f, "p", "p/x.md")}
    assert "stub_page" in codes


def test_check_file_detects_near_empty(tmp_path):
    f = tmp_path / "x.md"
    f.write_text("---\na: 1\n---\n\n#\n", encoding="utf-8")
    issues = vsd.check_file(f, "p", "p/x.md")
    assert any(i.code == "near_empty" for i in issues)


def test_check_file_clean_doc(tmp_path):
    f = tmp_path / "x.md"
    f.write_text("# Real Doc\n\n" + ("Substantial documentation content. " * 30) + "\nEnd.", encoding="utf-8")
    assert [i for i in vsd.check_file(f, "p", "p/x.md") if i.severity == "high"] == []


def test_is_content_md():
    assert vsd.is_content_md(Path("a/real-page.md")) is True
    assert vsd.is_content_md(Path("a/_index.md")) is False
    assert vsd.is_content_md(Path("a/index.md")) is False
    assert vsd.is_content_md(Path("a/_crawl_summary.json")) is False


# --------------------------- dir-level ------------------------------------- #
def test_check_provider_dir_stub_cluster_and_summary(tmp_path):
    root = tmp_path
    prov = root / "acme"
    prov.mkdir()
    # 3 identical-size small stubs (404 template)
    stub = "# Page not found\n\nThis page could not be found.\n"
    for n in ("a", "b", "c"):
        (prov / f"{n}.md").write_text(stub, encoding="utf-8")
    # one real, large doc
    (prov / "real.md").write_text("# Real\n\n" + ("payment flow detail. " * 200), encoding="utf-8")
    (prov / "_crawl_summary.json").write_text(json.dumps({"discovered": 6, "extracted": 4, "failed": 2, "errors": []}), encoding="utf-8")

    issues = vsd.check_provider_dir(prov, root)
    codes = {i.code for i in issues}
    assert "stub_cluster" in codes
    assert "stub_page" in codes
    assert "crawl_failed" in codes
    # the real doc raises no HIGH issue
    assert not any(i.path.endswith("real.md") and i.severity == "high" for i in issues)


def test_validate_clean_tree(tmp_path):
    prov = tmp_path / "good"
    prov.mkdir()
    (prov / "page.md").write_text("# Good\n\n" + ("complete content. " * 50) + "\nDone.", encoding="utf-8")
    assert vsd.validate(tmp_path) == []
