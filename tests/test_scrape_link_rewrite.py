"""
Tests for rewrite_href / sanitize_markdown_links in the scrape-docs pipeline.

Motivation: the 2026-04-23 link-rot audit found 9,807 broken internal links
(99.6% of all internal links) in scraped provider docs because Tavily's
markdown output preserved site-relative hrefs like `/foo/bar`. These are
garbage outside the original host. The crawler now rewrites them to absolute
URLs (or drops Docusaurus theme skip-links) before writing to disk.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


# Load the crawler module by path (it lives under profiles/pay-com/scripts/
# and has a hyphenated filename, so a normal import is awkward).
_CRAWLER_PATH = (
    Path(__file__).resolve().parent.parent
    / "profiles"
    / "pay-com"
    / "scripts"
    / "tavily-docs-crawler.py"
)


def _load_crawler():
    spec = importlib.util.spec_from_file_location(
        "tavily_docs_crawler", _CRAWLER_PATH
    )
    module = importlib.util.module_from_spec(spec)
    # Avoid importing the real tavily SDK during tests — stub it.
    if "tavily" not in sys.modules:
        import types

        stub = types.ModuleType("tavily")

        class _StubClient:  # pragma: no cover - never instantiated in tests
            def __init__(self, *a, **kw):
                raise RuntimeError("Tavily stub: not for network use")

        stub.TavilyClient = _StubClient
        sys.modules["tavily"] = stub
    spec.loader.exec_module(module)
    return module


crawler = _load_crawler()
rewrite_href = crawler.rewrite_href
sanitize_markdown_links = crawler.sanitize_markdown_links


BASE = "https://example.com/docs/"


class TestRewriteHref:
    def test_absolute_site_path_is_resolved(self):
        assert (
            rewrite_href("/foo/bar", "https://example.com/docs/")
            == "https://example.com/foo/bar"
        )

    def test_external_https_unchanged(self):
        assert (
            rewrite_href("https://external.com/a", BASE)
            == "https://external.com/a"
        )

    def test_external_http_unchanged(self):
        assert rewrite_href("http://plain.example/x", BASE) == "http://plain.example/x"

    def test_same_page_anchor_unchanged(self):
        assert rewrite_href("#anchor", BASE) == "#anchor"

    def test_docusaurus_skip_link_dropped(self):
        assert rewrite_href("#__docusaurus_skipToContent_fallback", BASE) == ""

    def test_content_skip_link_dropped(self):
        assert rewrite_href("#content", BASE) == ""

    def test_content_prefix_not_dropped(self):
        # "#content-foo" is a legitimate anchor; only exact "#content" is a skip link.
        assert rewrite_href("#content-foo", BASE) == "#content-foo"

    def test_mailto_unchanged(self):
        assert rewrite_href("mailto:x@y", BASE) == "mailto:x@y"

    def test_tel_unchanged(self):
        assert rewrite_href("tel:+123", BASE) == "tel:+123"

    def test_empty_href_unchanged(self):
        assert rewrite_href("", BASE) == ""

    def test_relative_path_unchanged(self):
        # Rare in extracted docs; leave as-is.
        assert rewrite_href("foo/bar", BASE) == "foo/bar"

    def test_site_path_uses_page_url_host(self):
        # Must use the scheme+host of the page URL, not guess.
        out = rewrite_href("/a", "https://docs.nuvei.com/deep/page/")
        assert out == "https://docs.nuvei.com/a"


class TestSanitizeMarkdownLinks:
    def test_rewrites_absolute_site_link(self):
        md = "See [Docs](/guide/intro) for details."
        out = sanitize_markdown_links(md, "https://example.com/docs/x")
        assert out == "See [Docs](https://example.com/guide/intro) for details."

    def test_leaves_external_link(self):
        md = "Visit [site](https://external.com/a)."
        assert sanitize_markdown_links(md, BASE) == md

    def test_drops_docusaurus_skip_link_keeps_text(self):
        md = "[Skip to main content](#__docusaurus_skipToContent_fallback)"
        out = sanitize_markdown_links(md, BASE)
        assert out == "Skip to main content"

    def test_preserves_image_syntax(self):
        # `![alt](/img.png)` should NOT be rewritten as a link — it's an image.
        md = "![alt](/img.png)"
        assert sanitize_markdown_links(md, BASE) == md

    def test_end_to_end_mixed(self):
        md = (
            "# Title\n\n"
            "[Home](/home)\n"
            "[External](https://ext.com/page)\n"
            "[Skip](#__docusaurus_skipToContent_fallback)\n"
            "[Anchor](#section)\n"
            "[Mail](mailto:a@b.com)\n"
        )
        out = sanitize_markdown_links(md, "https://docs.provider.com/api/v1/")
        assert "[Home](https://docs.provider.com/home)" in out
        assert "[External](https://ext.com/page)" in out
        # skip-link is flattened to plain text
        assert "Skip\n" in out
        assert "__docusaurus" not in out
        # regular anchors + mailto preserved
        assert "[Anchor](#section)" in out
        assert "[Mail](mailto:a@b.com)" in out

    def test_empty_input(self):
        assert sanitize_markdown_links("", BASE) == ""

    def test_no_links(self):
        md = "Just some plain text with no links at all."
        assert sanitize_markdown_links(md, BASE) == md


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
