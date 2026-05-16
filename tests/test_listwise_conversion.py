"""Tests for listwise data conversion + format detection (v8 FT prep)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from scripts.data.finetune_reranker import detect_listwise_format


class TestDetectListwiseFormat:
    def test_pointwise_data(self, tmp_path: Path):
        p = tmp_path / "pointwise.jsonl"
        p.write_text(
            '{"query": "q1", "document": "d1", "label": 1.0}\n{"query": "q2", "document": "d2", "label": 0.0}\n',
            encoding="utf-8",
        )
        assert detect_listwise_format(p) is False

    def test_listwise_data(self, tmp_path: Path):
        p = tmp_path / "listwise.jsonl"
        p.write_text(
            '{"query": "q1", "docs": ["d1", "d2"], "labels": [1.0, 0.0]}\n',
            encoding="utf-8",
        )
        assert detect_listwise_format(p) is True

    def test_empty_file_raises(self, tmp_path: Path):
        p = tmp_path / "empty.jsonl"
        p.write_text("", encoding="utf-8")
        with pytest.raises(ValueError, match="could not detect"):
            detect_listwise_format(p)

    def test_skips_blank_and_malformed_lines(self, tmp_path: Path):
        """First usable line wins — blanks and JSON errors skip to next."""
        p = tmp_path / "noisy.jsonl"
        p.write_text(
            "\n"  # blank
            "{broken json\n"  # malformed
            '{"irrelevant": "keys"}\n'  # missing both schemas — skip
            '{"query": "q", "docs": ["d"], "labels": [1.0]}\n',
            encoding="utf-8",
        )
        assert detect_listwise_format(p) is True


class TestConvertToListwiseScript:
    """End-to-end check that converter runs and produces sane listwise output."""

    _SCRIPT = _REPO_ROOT / "scripts" / "data" / "convert_to_listwise.py"

    def _make_pointwise(self, path: Path) -> None:
        rows = [
            # 2 pos + 2 neg for query A
            {"query": "A", "document": "docA1", "label": 1.0},
            {"query": "A", "document": "docA2", "label": 1.0},
            {"query": "A", "document": "docA3", "label": 0.0},
            {"query": "A", "document": "docA4", "label": 0.0},
            # 1 pos + 1 neg for query B
            {"query": "B", "document": "docB1", "label": 1.0},
            {"query": "B", "document": "docB2", "label": 0.0},
            # degenerate: only positives, no negs — should be skipped
            {"query": "C", "document": "docC1", "label": 1.0},
        ]
        path.write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n",
            encoding="utf-8",
        )

    def test_converter_produces_expected_groups(self, tmp_path: Path):
        in_path = tmp_path / "in.jsonl"
        out_path = tmp_path / "out.jsonl"
        self._make_pointwise(in_path)

        result = subprocess.run(
            [
                sys.executable,
                str(self._SCRIPT),
                "--in",
                str(in_path),
                "--out",
                str(out_path),
                "--max-negs",
                "10",
                "--max-docs-per-group",
                "32",
                "--seed",
                "42",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"

        rows = [json.loads(line) for line in out_path.read_text().splitlines() if line.strip()]
        # 2 valid queries (A, B), C degenerate → skipped
        assert len(rows) == 2
        # Sorted by query for determinism
        queries = [r["query"] for r in rows]
        assert queries == ["A", "B"]

        # Each row has docs + labels of same length
        for r in rows:
            assert len(r["docs"]) == len(r["labels"])
            # At least one positive and one negative
            assert any(lbl > 0.5 for lbl in r["labels"])
            assert any(lbl < 0.5 for lbl in r["labels"])

    def test_converter_caps_group_size(self, tmp_path: Path):
        """A 100-doc group must be truncated to --max-docs-per-group."""
        in_path = tmp_path / "in.jsonl"
        out_path = tmp_path / "out.jsonl"
        rows = []
        for i in range(60):
            rows.append({"query": "big", "document": f"pos{i}", "label": 1.0})
        for i in range(60):
            rows.append({"query": "big", "document": f"neg{i}", "label": 0.0})
        in_path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

        result = subprocess.run(
            [
                sys.executable,
                str(self._SCRIPT),
                "--in",
                str(in_path),
                "--out",
                str(out_path),
                "--max-negs",
                "31",
                "--max-docs-per-group",
                "32",
                "--seed",
                "42",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0

        out_rows = [json.loads(line) for line in out_path.read_text().splitlines() if line.strip()]
        assert len(out_rows) == 1
        g = out_rows[0]
        assert len(g["docs"]) == 32
        assert len(g["labels"]) == 32
        # Should still have at least 1 of each class
        n_pos = sum(1 for lbl in g["labels"] if lbl > 0.5)
        n_neg = sum(1 for lbl in g["labels"] if lbl < 0.5)
        assert n_pos >= 1
        assert n_neg >= 1
