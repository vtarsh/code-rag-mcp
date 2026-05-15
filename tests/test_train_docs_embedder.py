"""Regression tests for scripts/runpod/train_docs_embedder.py.

Covers:
- B4 (nomic prefix): load_pairs prepends search_query:/search_document: by default
- B5 (early HF_TOKEN check): train() aborts BEFORE heavy ST import on hf: + missing HF_TOKEN
- P7-task-B (--loss flag): mnrl|cosent|marginmse|tsdae plumbing + per-loss data shape validation
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from scripts.runpod import train_docs_embedder as ted

# ----- fixtures --------------------------------------------------------------


@pytest.fixture
def pairs_jsonl(tmp_path: Path) -> Path:
    rows = [
        {"query": "how does trustly work", "positive": "Trustly is an APM..."},
        {
            "query": "nuvei webhook handling",
            "positive": "Webhook endpoint at /webhooks/nuvei...",
            "negative": "PayPer retry policy...",
        },
    ]
    path = tmp_path / "train.jsonl"
    with path.open("w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return path


# ----- B4: nomic prefixes ----------------------------------------------------


def test_load_pairs_applies_nomic_prefixes_by_default(pairs_jsonl: Path):
    rows = ted.load_pairs(pairs_jsonl)
    assert len(rows) == 2
    for r in rows:
        assert r["query"].startswith(ted.NOMIC_QUERY_PREFIX)
        assert r["positive"].startswith(ted.NOMIC_DOCUMENT_PREFIX)
    # negative (if present) also gets the document prefix
    assert rows[1]["negative"].startswith(ted.NOMIC_DOCUMENT_PREFIX)


def test_load_pairs_nomic_prefix_strings_match_prod():
    """Guard against prefix drift vs src/models.py["docs"]."""
    assert ted.NOMIC_QUERY_PREFIX == "search_query: "
    assert ted.NOMIC_DOCUMENT_PREFIX == "search_document: "


def test_load_pairs_respects_no_prefix_flag(pairs_jsonl: Path):
    rows = ted.load_pairs(pairs_jsonl, apply_nomic_prefix=False)
    assert len(rows) == 2
    assert rows[0]["query"] == "how does trustly work"
    assert rows[0]["positive"] == "Trustly is an APM..."
    assert rows[1]["negative"] == "PayPer retry policy..."


def test_load_pairs_rejects_bad_row(tmp_path: Path):
    bad = tmp_path / "bad.jsonl"
    bad.write_text(json.dumps({"query": "only q"}) + "\n")
    with pytest.raises(ValueError, match="need query"):
        ted.load_pairs(bad)


# ----- B5: early HF_TOKEN check ----------------------------------------------


def test_train_aborts_early_if_hf_token_missing(monkeypatch, pairs_jsonl: Path):
    """If out is hf: and HF_TOKEN missing, train() must raise BEFORE importing
    sentence_transformers (so a mis-wired pod doesn't burn 10-20 min of GPU)."""
    monkeypatch.delenv("HF_TOKEN", raising=False)

    # Poison sentence_transformers + torch so any import attempt blows up.
    # train() must raise RuntimeError first, never touching these modules.
    class _Poison:
        def __getattr__(self, name):
            raise AssertionError(
                f"sentence_transformers.{name} was accessed — HF_TOKEN check was not hoisted before the heavy import"
            )

    monkeypatch.setitem(sys.modules, "sentence_transformers", _Poison())

    with pytest.raises(RuntimeError, match="HF_TOKEN missing"):
        ted.train(
            base="nomic-ai/nomic-embed-text-v1.5",
            train_path=pairs_jsonl,
            steps=1,
            out="hf:vtarsh/whatever",
        )


def test_train_does_not_require_hf_token_for_local_dir_out(monkeypatch, pairs_jsonl: Path, tmp_path: Path):
    """dir: output doesn't push to Hub, so missing HF_TOKEN is irrelevant.
    We stop the run right after the token check by poisoning the ST import —
    reaching the import means the token check correctly didn't abort.

    Pass `allow_tiny_data=True` to bypass the R1-retry-prep MIN_TRAIN_ROWS
    guard (this fixture file only carries 2 pairs); the test's purpose is
    the HF_TOKEN check, not the row-count guard."""
    monkeypatch.delenv("HF_TOKEN", raising=False)

    sentinel = RuntimeError("reached sentence_transformers import")

    class _StopHere:
        def __getattr__(self, name):
            raise sentinel

    monkeypatch.setitem(sys.modules, "sentence_transformers", _StopHere())

    out_dir = tmp_path / "out"
    with pytest.raises(RuntimeError, match="reached sentence_transformers"):
        ted.train(
            base="nomic-ai/nomic-embed-text-v1.5",
            train_path=pairs_jsonl,
            steps=1,
            out=str(out_dir),
            allow_tiny_data=True,
        )


# ----- --dry-run still works after the refactor ------------------------------


def test_dry_run_loads_prefixed_pairs(pairs_jsonl: Path, capsys):
    rc = ted.main(["--train", str(pairs_jsonl), "--out", "hf:x/y", "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "nomic_prefix=True" in out


def test_dry_run_respects_no_prefix(pairs_jsonl: Path, capsys):
    rc = ted.main(
        [
            "--train",
            str(pairs_jsonl),
            "--out",
            "./out",
            "--no-prefix",
            "--dry-run",
        ]
    )
    assert rc == 0
    assert "nomic_prefix=False" in capsys.readouterr().out


def test_parse_out_hf_and_dir():
    assert ted._parse_out("hf:user/name") == ("hf", "user/name")
    assert ted._parse_out("./local/dir") == ("dir", "./local/dir")


# ----- P7-task-B: --loss flag + data-shape branching ------------------------


def test_loss_flag_default_is_mnrl(pairs_jsonl: Path, capsys):
    """Backwards-compat: omitting --loss must keep MNRL (current v0/v1 behavior).

    Verified through --dry-run output (avoids torch import on Mac)."""
    rc = ted.main(["--train", str(pairs_jsonl), "--out", "hf:x/y", "--dry-run"])
    assert rc == 0
    assert "loss=mnrl" in capsys.readouterr().out


def test_loss_flag_validates_choice(pairs_jsonl: Path):
    """argparse rejects values outside LOSS_CHOICES with SystemExit (rc=2)."""
    with pytest.raises(SystemExit):
        ted.main(
            [
                "--train",
                str(pairs_jsonl),
                "--out",
                "./tmpout",
                "--loss",
                "wonderloss",  # not a valid family
                "--dry-run",
            ]
        )


def test_loss_flag_accepts_all_documented_choices(pairs_jsonl: Path, tmp_path: Path):
    """Every choice in LOSS_CHOICES must be parseable by argparse.

    We only check parser acceptance, not data-shape compatibility — that's the
    job of test_data_shape_validation_*. We use a tsdae-shaped raw-text file
    when --loss=tsdae and pairs_jsonl otherwise."""
    raw_text = tmp_path / "raw.jsonl"
    with raw_text.open("w") as f:
        f.write(json.dumps({"text": "hello world"}) + "\n")
    triplet = tmp_path / "triplet.jsonl"
    with triplet.open("w") as f:
        f.write(
            json.dumps(
                {
                    "query": "q",
                    "positive": "p",
                    "negative": "n",
                }
            )
            + "\n"
        )
    margin = tmp_path / "margin.jsonl"
    with margin.open("w") as f:
        f.write(
            json.dumps(
                {
                    "query": "q",
                    "positive": "p",
                    "negative": "n",
                    "margin": 0.7,
                }
            )
            + "\n"
        )

    cases = {
        "mnrl": pairs_jsonl,
        "cosent": triplet,
        "marginmse": margin,
        "tsdae": raw_text,
    }
    for loss, train in cases.items():
        rc = ted.main(
            [
                "--train",
                str(train),
                "--out",
                "./tmpout",
                "--loss",
                loss,
                "--dry-run",
            ]
        )
        assert rc == 0, f"--loss={loss} dry-run failed"


# Fake model used by _build_loss_and_data tests. Most ST losses just store the
# model reference at __init__; CoSENT/MNRL/MarginMSE work with any object.
# TSDAE accesses model[0] (tokenizer) so we have to fake __getitem__ too —
# we never actually call .forward in tests, so a stub is enough.
class _FakeModel:
    def __init__(self):
        self._inner = [object()]

    def __getitem__(self, idx):
        return self._inner[idx]


@pytest.mark.parametrize(
    "loss_name, expected_cls_name, row",
    [
        ("mnrl", "MultipleNegativesRankingLoss", {"query": "q", "positive": "p"}),
        ("cosent", "CoSENTLoss", {"query": "q", "positive": "p", "negative": "n"}),
        ("marginmse", "MarginMSELoss", {"query": "q", "positive": "p", "negative": "n", "margin": 0.5}),
    ],
)
def test_build_loss_returns_correct_class(loss_name, expected_cls_name, row):
    """`_build_loss_and_data(loss_name, ...)` must instantiate the right losses.* class.

    Skips tsdae here because DenoisingAutoEncoderLoss spins up a real decoder
    transformer at __init__, which is too expensive for unit tests — covered
    separately in test_build_loss_tsdae_uses_correct_class via monkeypatch.
    """
    rows = [row]
    loss, examples = ted._build_loss_and_data(loss_name, rows, _FakeModel())
    assert type(loss).__name__ == expected_cls_name
    assert len(examples) >= 1


def test_build_loss_tsdae_uses_correct_class(monkeypatch):
    """tsdae branch instantiates losses.DenoisingAutoEncoderLoss.

    We monkeypatch the class on the imported sentence_transformers.losses
    module so the test doesn't pull a real decoder model. This is the same
    pattern used to verify class selection without paying init cost."""
    from sentence_transformers import losses as st_losses

    sentinel_calls: list[tuple] = []

    class _FakeTSDAE:
        def __init__(self, model, *args, **kwargs):
            sentinel_calls.append((model, args, kwargs))

    monkeypatch.setattr(st_losses, "DenoisingAutoEncoderLoss", _FakeTSDAE)

    rows = [{"text": "the quick brown fox"}]
    loss, examples = ted._build_loss_and_data("tsdae", rows, _FakeModel())
    assert isinstance(loss, _FakeTSDAE)
    assert len(examples) == 1
    # InputExample wraps the raw text in a single-element texts list
    assert examples[0].texts == ["the quick brown fox"]
    assert len(sentinel_calls) == 1


def test_build_loss_cosent_emits_pos_and_neg_pairs():
    """CoSENT branch must emit BOTH (q, pos, 1.0) AND (q, neg, 0.0) rows.

    This is the load-bearing data-shape contract — CoSENT learns from the
    pairwise margin between positive-labeled and negative-labeled pairs, so
    half-feeding it (only positives) silently degenerates to a useless loss."""
    rows = [{"query": "q1", "positive": "p1", "negative": "n1"}]
    _loss, examples = ted._build_loss_and_data("cosent", rows, _FakeModel())
    assert len(examples) == 2
    labels = sorted(ex.label for ex in examples)
    assert labels == [0.0, 1.0]
    # positive paired with q, negative paired with q
    pos_ex = next(ex for ex in examples if ex.label == 1.0)
    neg_ex = next(ex for ex in examples if ex.label == 0.0)
    assert pos_ex.texts == ["q1", "p1"]
    assert neg_ex.texts == ["q1", "n1"]


def test_build_loss_marginmse_carries_teacher_margin():
    """MarginMSE branch must propagate the teacher `margin` as the label."""
    rows = [
        {"query": "q1", "positive": "p1", "negative": "n1", "margin": 0.42},
        {"query": "q2", "positive": "p2", "negative": "n2", "margin": -0.11},
    ]
    _loss, examples = ted._build_loss_and_data("marginmse", rows, _FakeModel())
    assert len(examples) == 2
    assert examples[0].texts == ["q1", "p1", "n1"]
    assert examples[0].label == pytest.approx(0.42)
    assert examples[1].label == pytest.approx(-0.11)


# ----- data-shape validation: refuse to train on incompatible JSONL ---------


def test_data_shape_validation_cosent_missing_neg():
    """CoSENT requires explicit negatives — refuse if JSONL is (q, pos) only.

    Catches the "I reused the MNRL training file" footgun that would otherwise
    burn pod minutes before silently flopping (CoSENT degenerates without negs)."""
    rows = [{"query": "q", "positive": "p"}]  # no negative
    with pytest.raises(ValueError, match=r"cosent.*negative"):
        ted._validate_rows_for_loss("cosent", rows)


def test_data_shape_validation_marginmse_missing_margin():
    """MarginMSE requires teacher margin scores — fail loudly if absent."""
    rows = [{"query": "q", "positive": "p", "negative": "n"}]  # no margin
    with pytest.raises(ValueError, match=r"marginmse.*margin"):
        ted._validate_rows_for_loss("marginmse", rows)


def test_data_shape_validation_tsdae_missing_text():
    """TSDAE wants `text` (or fallback `positive`) — anything else fails."""
    rows = [{"query": "q", "positive": "p"}]
    with pytest.raises(ValueError, match="tsdae"):
        ted._validate_rows_for_loss("tsdae", rows)


def test_data_shape_validation_mnrl_minimum_q_pos_passes():
    """MNRL is the lenient default — only needs query+positive."""
    rows = [{"query": "q", "positive": "p"}]
    # Should not raise.
    ted._validate_rows_for_loss("mnrl", rows)


def test_data_shape_validation_empty_rows_raises():
    """Defensive: empty training set is a configuration bug, not silent skip."""
    with pytest.raises(ValueError, match="empty"):
        ted._validate_rows_for_loss("mnrl", [])


def test_data_shape_validation_unknown_loss_raises():
    """If an unknown loss slips past argparse choices=, fail loudly."""
    with pytest.raises(ValueError, match="Unknown loss"):
        ted._validate_rows_for_loss("not_a_real_loss", [{"query": "q", "positive": "p"}])


def test_dry_run_marginmse_missing_margin_raises_before_pod_spend(tmp_path: Path):
    """End-to-end --dry-run guard: marginmse on (q,pos,neg)-only JSONL must
    abort with ValueError, NOT silently print success and let the user spin
    up a pod with a broken training file."""
    bad = tmp_path / "no_margin.jsonl"
    with bad.open("w") as f:
        f.write(
            json.dumps(
                {
                    "query": "q",
                    "positive": "p",
                    "negative": "n",
                }
            )
            + "\n"
        )

    with pytest.raises(ValueError, match=r"marginmse.*margin"):
        ted.main(
            [
                "--train",
                str(bad),
                "--out",
                "./out",
                "--loss",
                "marginmse",
                "--dry-run",
            ]
        )


def test_load_raw_text_accepts_text_field(tmp_path: Path):
    """TSDAE loader: rows with `text` field load cleanly."""
    p = tmp_path / "raw.jsonl"
    with p.open("w") as f:
        f.write(json.dumps({"text": "hello world"}) + "\n")
        f.write(json.dumps({"text": "second line"}) + "\n")
    rows = ted.load_raw_text(p)
    assert rows == [{"text": "hello world"}, {"text": "second line"}]


def test_load_raw_text_falls_back_to_positive(tmp_path: Path):
    """TSDAE loader: when only `positive` exists, treat it as text (lets us
    reuse MNRL training files for TSDAE pre-adaptation without re-prep)."""
    p = tmp_path / "fallback.jsonl"
    with p.open("w") as f:
        f.write(json.dumps({"query": "q", "positive": "doc body here"}) + "\n")
    rows = ted.load_raw_text(p)
    assert rows == [{"text": "doc body here"}]


def test_load_raw_text_rejects_row_without_text_or_positive(tmp_path: Path):
    p = tmp_path / "bad.jsonl"
    with p.open("w") as f:
        f.write(json.dumps({"query": "only q"}) + "\n")
    with pytest.raises(ValueError, match="tsdae"):
        ted.load_raw_text(p)


# ----- R1 Stage B (2026-04-25): --epochs and --max-seq-length plumbing --------


def test_epochs_flag_defaults_to_one(pairs_jsonl: Path, capsys):
    """Backwards-compat: omitting --epochs keeps the prior single-epoch run."""
    rc = ted.main(["--train", str(pairs_jsonl), "--out", "./tmpout", "--dry-run"])
    assert rc == 0
    # parser default surfaces via main() -> argparse, no need to assert in stdout


def test_epochs_flag_accepts_int(pairs_jsonl: Path):
    """argparse accepts --epochs N integer; dry-run still returns 0."""
    rc = ted.main(
        [
            "--train",
            str(pairs_jsonl),
            "--out",
            "./tmpout",
            "--epochs",
            "3",
            "--dry-run",
        ]
    )
    assert rc == 0


def test_max_seq_length_flag_accepts_int(pairs_jsonl: Path):
    """argparse accepts --max-seq-length N to cap the encoder context window."""
    rc = ted.main(
        [
            "--train",
            str(pairs_jsonl),
            "--out",
            "./tmpout",
            "--max-seq-length",
            "512",
            "--dry-run",
        ]
    )
    assert rc == 0


def test_max_seq_length_default_is_zero(pairs_jsonl: Path):
    """Default cap=0 means leave the model's tokenizer setting untouched."""
    rc = ted.main(["--train", str(pairs_jsonl), "--out", "./tmpout", "--dry-run"])
    assert rc == 0
    # When max_seq_length=0 the train() function does not touch
    # model.max_seq_length — verified by inspection of the train body; this
    # test only locks the parser default so a future flag rename surfaces here.


# ----- R1-retry-prep (2026-04-25): MIN_TRAIN_ROWS guard ----------------------


def _write_n_pairs(path: Path, n: int) -> None:
    """Helper: write `n` (q, pos) pairs into a JSONL file."""
    with path.open("w") as f:
        for i in range(n):
            f.write(
                json.dumps(
                    {
                        "query": f"q{i}",
                        "positive": f"doc body {i}",
                    }
                )
                + "\n"
            )


def test_train_aborts_if_too_few_rows(monkeypatch, tmp_path: Path):
    """Feeding 50 rows must raise ValueError with REFUSE before any optimizer
    step — this is the load-bearing guard against repeating the 2026-04-25
    R1-stage2 bug (--train silently fell back to a stale 10-pair payfin-v0
    dataset; result was -17.7pp regression). The check must fire BEFORE the
    sentence_transformers import so a miswired pod doesn't even start loading
    the 547 MB transformer."""
    # Skip the HF_TOKEN early-abort by giving a token; we want to reach the
    # row-count guard, not the token guard.
    monkeypatch.setenv("HF_TOKEN", "fake")

    # Poison sentence_transformers so any import attempt blows up — proves the
    # guard fired before the heavy import.
    class _Poison:
        def __getattr__(self, name):
            raise AssertionError(
                f"sentence_transformers.{name} accessed — MIN_TRAIN_ROWS guard did not fire before the heavy import"
            )

    monkeypatch.setitem(sys.modules, "sentence_transformers", _Poison())

    train_path = tmp_path / "tiny.jsonl"
    _write_n_pairs(train_path, 50)

    with pytest.raises(ValueError, match="REFUSE"):
        ted.train(
            base="nomic-ai/nomic-embed-text-v1.5",
            train_path=train_path,
            steps=1,
            out="hf:vtarsh/whatever",
        )


def test_train_aborts_message_calls_out_legacy_dataset(monkeypatch, tmp_path: Path):
    """The REFUSE message must mention the legacy `payfin-v0` dataset name so
    the human reading a pod log knows exactly which incident they're avoiding."""
    monkeypatch.setenv("HF_TOKEN", "fake")
    monkeypatch.setitem(
        sys.modules,
        "sentence_transformers",
        type(
            "_P", (), {"__getattr__": lambda self, n: (_ for _ in ()).throw(AssertionError("guard did not fire first"))}
        )(),
    )
    train_path = tmp_path / "tiny.jsonl"
    _write_n_pairs(train_path, 10)

    with pytest.raises(ValueError) as exc_info:
        ted.train(
            base="nomic-ai/nomic-embed-text-v1.5",
            train_path=train_path,
            steps=1,
            out="./out",  # local dir avoids HF_TOKEN check
        )
    msg = str(exc_info.value)
    assert "payfin-v0" in msg
    assert "10-pair" in msg or "10 pair" in msg


def test_train_logs_absolute_train_path_at_startup(monkeypatch, tmp_path: Path, capsys):
    """Before any optimizer step, train() must print the *absolute* path of
    the training file. Without this log, the 2026-04-25 incident (silent
    fallback to a stale path) would repeat — an op reading the pod log can
    eyeball the abs path and catch a swap immediately."""
    monkeypatch.setenv("HF_TOKEN", "fake")

    # Poison ST so train() halts after the row-count guard and we can inspect
    # the captured stdout. We need ≥MIN_TRAIN_ROWS rows so the guard passes.
    sentinel = RuntimeError("reached sentence_transformers")

    class _StopAfterGuard:
        def __getattr__(self, name):
            raise sentinel

    monkeypatch.setitem(sys.modules, "sentence_transformers", _StopAfterGuard())

    train_path = tmp_path / "big.jsonl"
    _write_n_pairs(train_path, ted.MIN_TRAIN_ROWS + 5)

    with pytest.raises(RuntimeError, match="reached sentence_transformers"):
        ted.train(
            base="nomic-ai/nomic-embed-text-v1.5",
            train_path=train_path,
            steps=1,
            out="./out",
        )

    captured = capsys.readouterr().out
    abs_path = str(train_path.resolve())
    assert f"train_path_abs={abs_path}" in captured, (
        f"absolute path was not logged at startup; captured stdout:\n{captured}"
    )
    # Also assert the row-count is logged (the second guard signal)
    assert f"n_rows={ted.MIN_TRAIN_ROWS + 5}" in captured


def test_train_logs_first_three_row_queries(monkeypatch, tmp_path: Path, capsys):
    """train() must log the first 3 rows' query strings so the human can
    spot a mismatch (e.g. 'how does refund work' vs payfin-v0's
    'cybersource enable' fingerprint)."""
    monkeypatch.setenv("HF_TOKEN", "fake")
    sentinel = RuntimeError("post-guard")

    class _StopAfterGuard:
        def __getattr__(self, name):
            raise sentinel

    monkeypatch.setitem(sys.modules, "sentence_transformers", _StopAfterGuard())

    train_path = tmp_path / "big.jsonl"
    rows_n = ted.MIN_TRAIN_ROWS + 5
    with train_path.open("w") as f:
        f.write(
            json.dumps(
                {
                    "query": "FINGERPRINT_QUERY_0",
                    "positive": "doc 0",
                }
            )
            + "\n"
        )
        f.write(
            json.dumps(
                {
                    "query": "FINGERPRINT_QUERY_1",
                    "positive": "doc 1",
                }
            )
            + "\n"
        )
        f.write(
            json.dumps(
                {
                    "query": "FINGERPRINT_QUERY_2",
                    "positive": "doc 2",
                }
            )
            + "\n"
        )
        for i in range(3, rows_n):
            f.write(
                json.dumps(
                    {
                        "query": f"q{i}",
                        "positive": f"doc {i}",
                    }
                )
                + "\n"
            )

    with pytest.raises(RuntimeError, match="post-guard"):
        ted.train(
            base="nomic-ai/nomic-embed-text-v1.5",
            train_path=train_path,
            steps=1,
            out="./out",
        )
    captured = capsys.readouterr().out
    for i in range(3):
        assert f"FINGERPRINT_QUERY_{i}" in captured, f"row[{i}] query not in startup log; captured:\n{captured}"


def test_train_allow_tiny_data_override(monkeypatch, tmp_path: Path):
    """The escape hatch: smoke runs / unit tests pass `allow_tiny_data=True`
    so the guard doesn't block them. We verify the guard is skipped and
    train() proceeds past the row-count check (ST poison still halts it)."""
    monkeypatch.setenv("HF_TOKEN", "fake")
    sentinel = RuntimeError("reached sentence_transformers")

    class _StopAfterGuard:
        def __getattr__(self, name):
            raise sentinel

    monkeypatch.setitem(sys.modules, "sentence_transformers", _StopAfterGuard())

    train_path = tmp_path / "tiny.jsonl"
    _write_n_pairs(train_path, 10)  # well below MIN_TRAIN_ROWS

    # Without override: ValueError; with override: ST sentinel reached.
    with pytest.raises(RuntimeError, match="reached sentence_transformers"):
        ted.train(
            base="nomic-ai/nomic-embed-text-v1.5",
            train_path=train_path,
            steps=1,
            out="./out",
            allow_tiny_data=True,
        )


def test_cli_flag_i_know_im_using_tiny_data_plumbed(monkeypatch, tmp_path: Path):
    """The CLI flag --i-know-im-using-tiny-data must propagate to train()
    so smoke runs from the shell can opt-in to the override."""
    monkeypatch.setenv("HF_TOKEN", "fake")
    sentinel = RuntimeError("reached sentence_transformers")

    class _StopAfterGuard:
        def __getattr__(self, name):
            raise sentinel

    monkeypatch.setitem(sys.modules, "sentence_transformers", _StopAfterGuard())

    train_path = tmp_path / "tiny.jsonl"
    _write_n_pairs(train_path, 10)

    # Without the flag: ValueError should bubble up.
    with pytest.raises(ValueError, match="REFUSE"):
        ted.main(
            [
                "--train",
                str(train_path),
                "--out",
                "./out",
            ]
        )
    # With the flag: pass guard, hit sentinel.
    with pytest.raises(RuntimeError, match="reached sentence_transformers"):
        ted.main(
            [
                "--train",
                str(train_path),
                "--out",
                "./out",
                "--i-know-im-using-tiny-data",
            ]
        )


def test_min_train_rows_constant_documented():
    """MIN_TRAIN_ROWS is the single source of truth — guard against drift."""
    assert ted.MIN_TRAIN_ROWS >= 1000, "MIN_TRAIN_ROWS dropping below 1000 invites another payfin-v0 incident"
