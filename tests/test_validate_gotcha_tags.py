"""Tests for scripts/maint/validate_gotcha_tags.py."""

from pathlib import Path

from scripts.maint.validate_gotcha_tags import _extract_tags, _load_dictionary_names, main


class TestLoadDictionaryNames:
    def test_not_empty(self):
        names = _load_dictionary_names()
        assert len(names) > 0

    def test_concepts_present(self):
        names = _load_dictionary_names()
        assert "throw_policy" in names

    def test_field_aliases_present(self):
        names = _load_dictionary_names()
        assert "auth_code" in names
        assert "authcode" in names


class TestExtractTags:
    def test_with_tags(self, tmp_path: Path):
        md = tmp_path / "test.md"
        md.write_text("---\ntype: gotcha\ntags: [apm, webhook]\n---\n# Title\n")
        assert _extract_tags(md) == ["apm", "webhook"]

    def test_no_tags(self, tmp_path: Path):
        md = tmp_path / "test.md"
        md.write_text("---\ntype: gotcha\n---\n# Title\n")
        assert _extract_tags(md) is None

    def test_no_frontmatter(self, tmp_path: Path):
        md = tmp_path / "test.md"
        md.write_text("# Title\n")
        assert _extract_tags(md) is None

    def test_empty_tags(self, tmp_path: Path):
        md = tmp_path / "test.md"
        md.write_text("---\ntype: gotcha\ntags: []\n---\n# Title\n")
        assert _extract_tags(md) == []


class TestMain:
    def test_exits_zero(self):
        assert main() == 0
