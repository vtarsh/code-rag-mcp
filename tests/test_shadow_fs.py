"""Tests for shadow filesystem tools (grep_shadow / read_shadow_file / list_shadow_dir)."""

from __future__ import annotations

import json

import pytest

from src.tools import shadow_fs


@pytest.fixture()
def shadow(tmp_path, monkeypatch):
    """Minimal extracted/ tree: two repos + _index.json with shas."""
    repo_a = tmp_path / "grpc-apm-demo" / "methods"
    repo_a.mkdir(parents=True)
    (repo_a / "initialize.js").write_text(
        "const x = 1\nconst isCheckoutFlow = paymentMethod.type === 'volt'\nmodule.exports = x\n"
    )
    repo_b = tmp_path / "express-api-demo"
    repo_b.mkdir()
    (repo_b / "index.js").write_text("// nothing here\n")
    (tmp_path / "_index.json").write_text(json.dumps({"grpc-apm-demo": {"sha": "abcdef1234567890"}}))

    monkeypatch.setattr(shadow_fs, "EXTRACTED_DIR", tmp_path)
    monkeypatch.setattr(shadow_fs, "_repo_sha_cache", None)
    return tmp_path


def test_grep_finds_match_with_repo_scope(shadow):
    out = shadow_fs.grep_shadow_tool("isCheckoutFlow", repo="grpc-apm-demo")
    assert "grpc-apm-demo/methods/initialize.js:2" in out
    assert "paymentMethod.type" in out


def test_grep_all_repos_and_no_match(shadow):
    out = shadow_fs.grep_shadow_tool("isCheckoutFlow")
    assert "initialize.js:2" in out
    assert "No matches" in shadow_fs.grep_shadow_tool("definitely-not-there-xyz")


def test_grep_unknown_repo_suggests(shadow):
    out = shadow_fs.grep_shadow_tool("x", repo="grpc-apm-dem")
    assert "Did you mean" in out
    assert "grpc-apm-demo" in out


def test_grep_fixed_string_and_glob(shadow):
    out = shadow_fs.grep_shadow_tool("paymentMethod.type === 'volt'", glob="*.js", fixed_string=True)
    assert "initialize.js:2" in out
    assert "No matches" in shadow_fs.grep_shadow_tool("isCheckoutFlow", glob="*.py")


def test_read_file_with_offset_limit_and_sha(shadow):
    out = shadow_fs.read_shadow_file_tool("grpc-apm-demo/methods/initialize.js", offset=2, limit=1)
    assert "@ abcdef123" in out
    assert "lines 2-2 of 3" in out
    assert "     2\tconst isCheckoutFlow" in out
    assert "module.exports" not in out


def test_read_file_errors(shadow):
    assert "is a directory" in shadow_fs.read_shadow_file_tool("grpc-apm-demo/methods")
    assert "File not found" in shadow_fs.read_shadow_file_tool("grpc-apm-demo/methods/nope.js")
    assert "Did you mean" in shadow_fs.read_shadow_file_tool("grpc-apm-dem/methods/nope.js")
    assert "past EOF" in shadow_fs.read_shadow_file_tool("grpc-apm-demo/methods/initialize.js", offset=99)


def test_path_traversal_blocked(shadow):
    assert "escapes" in shadow_fs.read_shadow_file_tool("../outside.txt")
    assert "escapes" in shadow_fs.list_shadow_dir_tool("../../etc")


def test_list_dir_root_and_nested(shadow):
    root = shadow_fs.list_shadow_dir_tool("")
    assert "grpc-apm-demo/" in root
    assert "express-api-demo/" in root
    assert "_index.json" not in root

    nested = shadow_fs.list_shadow_dir_tool("grpc-apm-demo/methods")
    assert "initialize.js" in nested
