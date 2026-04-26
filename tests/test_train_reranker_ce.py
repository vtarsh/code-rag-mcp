"""Tests for scripts/runpod/train_reranker_ce.py.

The cross-encoder trainer is the missing partner of train_docs_embedder.py
that full_pipeline.py routes ``kind="reranker"`` jobs to. We mock
``CrossEncoder`` so the test stays offline + fast (no torch / no ST weights).

Coverage:
- CLI parses (mirrors train_docs_embedder.py CLI shape)
- MIN_TRAIN_ROWS guard fires on <1000 rows (with override escape)
- artifact path resolution: hf:owner/name -> ("hf", "owner/name");
  any other string -> ("dir", that string)
- early HF_TOKEN check (parity with train_docs_embedder)
- load_pairs accepts {query, doc, label[, source]} and rejects malformed rows
- happy-path train() under a fully-mocked CrossEncoder; correct out-routing
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from scripts.runpod import train_reranker_ce as trc

# ----- fixtures --------------------------------------------------------------


@pytest.fixture
def tiny_pairs(tmp_path: Path) -> Path:
    """Two-row JSONL — used to exercise the tiny-data guard."""
    rows = [
        {"query": "q1", "doc": "d1", "label": 1, "source": "code"},
        {"query": "q2", "doc": "d2", "label": 0, "source": "docs"},
    ]
    path = tmp_path / "tiny.jsonl"
    with path.open("w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return path


@pytest.fixture
def big_pairs(tmp_path: Path) -> Path:
    """1500-row JSONL — comfortably above MIN_TRAIN_ROWS=1000.

    Half labeled 1, half labeled 0 so a downstream `_build_examples` smoke
    sees both polarities even when shuffled.
    """
    path = tmp_path / "big.jsonl"
    with path.open("w") as f:
        for i in range(1500):
            label = 1 if i % 2 == 0 else 0
            f.write(
                json.dumps(
                    {
                        "query": f"q{i}",
                        "doc": f"d{i}",
                        "label": label,
                        "source": "code" if i % 3 == 0 else "docs",
                    }
                )
                + "\n"
            )
    return path


# ----- _parse_out -----------------------------------------------------------


def test_parse_out_hf_prefix_strips_to_target():
    kind, target = trc._parse_out("hf:vtarsh/pay-rerank-v1")
    assert kind == "hf"
    assert target == "vtarsh/pay-rerank-v1"


def test_parse_out_local_dir():
    kind, target = trc._parse_out("/tmp/some/local/dir")
    assert kind == "dir"
    assert target == "/tmp/some/local/dir"


def test_parse_out_parity_with_docs_trainer():
    """If this assertion ever fails, the docs and reranker trainers' output
    routing has drifted — fix BOTH or the pipeline lifecycle breaks."""
    from scripts.runpod import train_docs_embedder as ted

    assert trc.HF_OUT_PREFIX == ted.HF_OUT_PREFIX


# ----- CLI parsing ----------------------------------------------------------


def test_cli_parses_required_args(tmp_path: Path):
    p = trc._build_argparser()
    ns = p.parse_args(
        [
            "--base=cross-encoder/ms-marco-MiniLM-L-6-v2",
            f"--train={tmp_path / 'x.jsonl'}",
            "--steps=200",
            "--out=/tmp/out_dir",
            "--batch-size=32",
            "--lr=3e-5",
            "--epochs=2",
        ]
    )
    assert ns.base == "cross-encoder/ms-marco-MiniLM-L-6-v2"
    assert ns.steps == 200
    assert ns.batch_size == 32
    assert ns.lr == pytest.approx(3e-5)
    assert ns.epochs == 2
    assert ns.loss == "bce"  # default


def test_cli_default_loss_is_bce():
    p = trc._build_argparser()
    ns = p.parse_args(["--train=x", "--out=y"])
    assert ns.loss == "bce"


def test_cli_loss_choices_lambda_and_bce():
    p = trc._build_argparser()
    ns = p.parse_args(["--train=x", "--out=y", "--loss=lambda"])
    assert ns.loss == "lambda"


def test_cli_rejects_unknown_loss():
    p = trc._build_argparser()
    with pytest.raises(SystemExit):
        p.parse_args(["--train=x", "--out=y", "--loss=hinge"])


# ----- load_pairs -----------------------------------------------------------


def test_load_pairs_accepts_full_schema(big_pairs: Path):
    rows = trc.load_pairs(big_pairs)
    assert len(rows) == 1500
    for r in rows:
        assert set(r.keys()) == {"query", "doc", "label"}
        assert r["label"] in (0, 1)


def test_load_pairs_rejects_missing_label(tmp_path: Path):
    bad = tmp_path / "bad.jsonl"
    bad.write_text(json.dumps({"query": "q", "doc": "d"}) + "\n")
    with pytest.raises(ValueError, match="need query"):
        trc.load_pairs(bad)


def test_load_pairs_rejects_non_binary_label(tmp_path: Path):
    bad = tmp_path / "bad.jsonl"
    bad.write_text(json.dumps({"query": "q", "doc": "d", "label": 0.5}) + "\n")
    with pytest.raises(ValueError, match="0 or 1"):
        trc.load_pairs(bad)


# ----- early HF_TOKEN check -------------------------------------------------


def test_train_aborts_early_if_hf_token_missing(monkeypatch, tiny_pairs: Path):
    """Mirror of train_docs_embedder's B5 contract: hf: out + missing HF_TOKEN
    must raise BEFORE the heavy CrossEncoder import."""
    monkeypatch.delenv("HF_TOKEN", raising=False)

    class _Poison:
        def __getattr__(self, name):
            raise AssertionError(f"sentence_transformers.{name} accessed despite missing HF_TOKEN")

    monkeypatch.setitem(sys.modules, "sentence_transformers", _Poison())

    with pytest.raises(RuntimeError, match="HF_TOKEN missing"):
        trc.train(
            base="cross-encoder/ms-marco-MiniLM-L-6-v2",
            train_path=tiny_pairs,
            steps=1,
            out="hf:vtarsh/whatever",
        )


# ----- MIN_TRAIN_ROWS guard -------------------------------------------------


def test_train_refuses_tiny_data_without_override(monkeypatch, tiny_pairs: Path, tmp_path: Path):
    """<1000 rows + no override -> ValueError, no CrossEncoder ever loaded."""
    monkeypatch.setenv("HF_TOKEN", "stub")  # avoid the earlier guard

    class _Poison:
        def __getattr__(self, name):
            raise AssertionError(f"sentence_transformers.{name} accessed despite tiny-data guard")

    monkeypatch.setitem(sys.modules, "sentence_transformers", _Poison())

    with pytest.raises(ValueError, match="REFUSE: training data"):
        trc.train(
            base="cross-encoder/ms-marco-MiniLM-L-6-v2",
            train_path=tiny_pairs,
            steps=1,
            out=str(tmp_path / "out"),
        )


def test_min_train_rows_constant_matches_docs_trainer():
    """The two trainers must share the same minimum-rows floor or one will
    silently accept data the other rejects, which defeats the guard."""
    from scripts.runpod import train_docs_embedder as ted

    assert trc.MIN_TRAIN_ROWS == ted.MIN_TRAIN_ROWS == 1000


# ----- happy-path train() under a mocked CrossEncoder -----------------------


def test_train_happy_path_local_dir(monkeypatch, big_pairs: Path, tmp_path: Path):
    """train() with >=1000 rows and a local-dir out should:
    * call CrossEncoder(base, num_labels=1, ...)
    * call .fit() with a DataLoader-shaped train_dataloader
    * call .save(out_dir)
    """
    out_dir = tmp_path / "rerank_local"
    fake_model = MagicMock()
    # InputExample is referenced inside _build_examples — inject a stub.
    fake_input_example = MagicMock(side_effect=lambda **kw: SimpleNamespace(**kw))
    fake_st_module = SimpleNamespace(
        CrossEncoder=MagicMock(return_value=fake_model),
        InputExample=fake_input_example,
    )
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_st_module)

    trc.train(
        base="cross-encoder/ms-marco-MiniLM-L-6-v2",
        train_path=big_pairs,
        steps=10,
        out=str(out_dir),
        batch_size=4,
        learning_rate=1e-5,
        epochs=1,
    )

    # CrossEncoder constructor must be called with num_labels=1 (BCE-friendly head).
    fake_st_module.CrossEncoder.assert_called_once()
    args, kwargs = fake_st_module.CrossEncoder.call_args
    assert args[0] == "cross-encoder/ms-marco-MiniLM-L-6-v2"
    assert kwargs.get("num_labels") == 1

    # .fit must receive train_dataloader, epochs, optimizer_params, etc.
    fake_model.fit.assert_called_once()
    fit_kwargs = fake_model.fit.call_args.kwargs
    assert "train_dataloader" in fit_kwargs
    assert fit_kwargs["epochs"] == 1
    assert fit_kwargs["optimizer_params"] == {"lr": 1e-5}
    # steps_per_epoch should be set because we passed steps=10 (> 0).
    assert fit_kwargs["steps_per_epoch"] == 10

    # Local dir route: model.save(out_dir) must run, no push_to_hub.
    fake_model.save.assert_called_once_with(str(out_dir))
    fake_model.push_to_hub.assert_not_called()


def test_train_happy_path_hf_push(monkeypatch, big_pairs: Path):
    """train() with `hf:owner/name` out should call model.push_to_hub.

    HF_TOKEN must be present (otherwise the earlier guard raises). We rely on
    the mocked CrossEncoder — no real Hub call is made.
    """
    monkeypatch.setenv("HF_TOKEN", "stub")
    fake_model = MagicMock()
    fake_model.push_to_hub = MagicMock()
    fake_input_example = MagicMock(side_effect=lambda **kw: SimpleNamespace(**kw))
    fake_st_module = SimpleNamespace(
        CrossEncoder=MagicMock(return_value=fake_model),
        InputExample=fake_input_example,
    )
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_st_module)

    trc.train(
        base="cross-encoder/ms-marco-MiniLM-L-6-v2",
        train_path=big_pairs,
        steps=0,  # full-pass training (no steps_per_epoch)
        out="hf:vtarsh/pay-rerank-v1",
        epochs=2,
    )
    fake_model.push_to_hub.assert_called_once_with("vtarsh/pay-rerank-v1", private=True)
    # Confirm steps=0 maps to "no steps_per_epoch" — full-data pass per epoch.
    fit_kwargs = fake_model.fit.call_args.kwargs
    assert "steps_per_epoch" not in fit_kwargs
    assert fit_kwargs["epochs"] == 2


# ----- main() smoke ---------------------------------------------------------


def test_main_dry_run_returns_zero(big_pairs: Path, capsys, tmp_path: Path):
    """--dry-run loads the pairs and prints a stats line; no training."""
    rc = trc.main(
        [
            f"--train={big_pairs}",
            f"--out={tmp_path / 'out'}",
            "--dry-run",
        ]
    )
    assert rc == 0
    captured = capsys.readouterr()
    assert "DRY RUN" in captured.out
    assert "1500 rows" in captured.out
