#!/usr/bin/env python3
"""Fine-tune a CrossEncoder reranker on combined (query, doc, label) pairs.

Mirrors the CLI shape of ``scripts/runpod/train_docs_embedder.py`` so the
``full_pipeline.py`` runner can reuse the same arg-string-building code path,
but trains a *cross-encoder* head with binary BCE loss instead of a bi-encoder
sentence-transformer.

Usage on pod::

    python3 train_reranker_ce.py \
        --base=cross-encoder/ms-marco-MiniLM-L-6-v2 \
        --train=/workspace/combined_train.jsonl \
        --steps=0 \
        --epochs=2 \
        --out=hf:vtarsh/pay-com-rerank-v1

JSONL row format (built by ``scripts/build_combined_train.py``)::

    {"query": str, "doc": str, "label": 0|1, "source": "code"|"docs"}

Loss families:
    bce     (default): standard sigmoid + BCE — matches CrossEncoder.fit()'s
                       built-in ``loss_fct=None`` head (BCEWithLogitsLoss).
    lambda            : LambdaRankLoss — only emitted if the user opts in via
                       ``--loss=lambda`` AND a query-grouped dataset is built;
                       otherwise we fall back to BCE so a pod doesn't burn
                       GPU on a misconfigured run.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Final

DEFAULT_BASE: Final = "cross-encoder/ms-marco-MiniLM-L-6-v2"
HF_OUT_PREFIX: Final = "hf:"

# Mirror the bi-encoder trainer's MIN_TRAIN_ROWS guard: any train file with
# fewer than this many rows is almost certainly a stale or smoke-only fixture
# silently substituted into a real run. The override
# ``--i-know-im-using-tiny-data`` exists ONLY for unit tests / smoke runs.
# See `scripts/runpod/train_docs_embedder.py` MIN_TRAIN_ROWS docstring for the
# 2026-04-25 R1-stage2 -17.7pp regression that motivated the guard.
MIN_TRAIN_ROWS: Final = 1000

LOSS_CHOICES: Final = ("bce", "lambda")
DEFAULT_LOSS: Final = "bce"


def _parse_out(out: str) -> tuple[str, str]:
    """Return ``(kind, target)`` where ``kind`` is ``"hf"`` or ``"dir"``.

    Identical to ``train_docs_embedder._parse_out`` so the two trainers'
    output-routing semantics stay in lockstep — a refactor that touches one
    must touch the other (see test_train_reranker_ce for the parity assertion).
    """
    if out.startswith(HF_OUT_PREFIX):
        return "hf", out[len(HF_OUT_PREFIX) :]
    return "dir", out


def load_pairs(path: Path) -> list[dict]:
    """Load JSONL rows with ``{query, doc, label}`` (label coerced to int).

    Tolerates an extra ``source`` field (set by the combined-train builder)
    and silently drops anything else. Bad rows raise loud — tests rely on the
    explicit ValueError so a misshapen file doesn't get pushed to a pod.
    """
    rows: list[dict] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if "query" not in row or "doc" not in row or "label" not in row:
                raise ValueError(f"Bad row (need query+doc+label): {row}")
            # Accept int 0/1 OR float 0.0/1.0 (the v8 fixture writes floats).
            # Reject anything else loud — a 0.5 sneaking through would mean
            # the upstream label-smoothing branch silently leaked into the
            # binary-CE training set.
            raw_label = row["label"]
            label_float = float(raw_label)
            if label_float not in (0.0, 1.0):
                raise ValueError(f"label must be 0 or 1 (got {raw_label!r})")
            rows.append(
                {
                    "query": row["query"],
                    "doc": row["doc"],
                    "label": int(label_float),
                }
            )
    return rows


def _build_examples(rows: list[dict]) -> list[Any]:
    """Wrap rows into ``InputExample`` instances for ``CrossEncoder.fit()``.

    Lazy import keeps the module cheap to import on Mac / in tests where
    sentence_transformers may be a heavyweight dependency.
    """
    from sentence_transformers import InputExample

    return [InputExample(texts=[r["query"], r["doc"]], label=float(r["label"])) for r in rows]


# ----- training entrypoint ---------------------------------------------------


def train(
    base: str,
    train_path: Path,
    steps: int,
    out: str,
    *,
    batch_size: int = 16,
    learning_rate: float = 2e-5,
    epochs: int = 1,
    max_seq_length: int = 0,
    loss_name: str = DEFAULT_LOSS,
    allow_tiny_data: bool = False,
) -> None:
    """Run the cross-encoder fine-tune.

    Heavy deps (``sentence_transformers``, ``torch``) imported lazily so this
    file can be imported in tests on a Mac without the GPU stack. The unit
    test suite patches ``sentence_transformers.CrossEncoder`` to a Mock to
    verify the argument plumbing without actually loading 100s of MB of
    weights.

    Contract mirrored from ``train_docs_embedder.train``:
      * HF_TOKEN check before the heavy import (early failure on misconfig).
      * MIN_TRAIN_ROWS guard with ``allow_tiny_data`` escape (smoke/tests).
      * Visible row-count + first-3-row queries log.
    """
    # HF_TOKEN check BEFORE heavy import — same Bug B5 contract as
    # train_docs_embedder. A missing token on hf: output otherwise burns
    # 10-20 min of GPU before the push raises.
    kind, target = _parse_out(out)
    if kind == "hf" and not os.getenv("HF_TOKEN"):
        raise RuntimeError("HF_TOKEN missing — required for hf: push")

    abs_train_path = Path(train_path).resolve()
    print(f"[train_reranker] train_path_abs={abs_train_path!s}", flush=True)

    rows = load_pairs(train_path)
    n_rows = len(rows)
    print(
        f"[train_reranker] n_rows={n_rows} loaded from {abs_train_path}",
        flush=True,
    )
    for i, row in enumerate(rows[:3]):
        print(
            f"[train_reranker] row[{i}].query={row['query'][:120]!r} label={row['label']}",
            flush=True,
        )

    # Hard-refuse tiny data (unless explicitly authorised). Same arithmetic
    # as the docs-tower trainer: <1000 rows is almost certainly a stale/smoke
    # fixture mistakenly substituted into a real R1 retry run.
    if n_rows < MIN_TRAIN_ROWS and not allow_tiny_data:
        raise ValueError(
            f"REFUSE: training data {abs_train_path!s} has only {n_rows} rows "
            f"(need >= {MIN_TRAIN_ROWS}). Pass --i-know-im-using-tiny-data "
            f"only for smoke/unit runs."
        )

    print(
        f"[train_reranker] {n_rows} rows; base={base}; steps={steps}; "
        f"epochs={epochs}; out={out}; bs={batch_size}; lr={learning_rate}; "
        f"loss={loss_name}; max_seq_length={max_seq_length or 'default'}",
        flush=True,
    )

    # ---- heavy imports (after early-fail guards) -------------------------
    from sentence_transformers import CrossEncoder
    from torch.utils.data import DataLoader

    # CrossEncoder defaults to a single-label regression head; we want a
    # 1-logit binary classifier so BCEWithLogitsLoss (the default loss) sees
    # what it expects. num_labels=1 is the standard MS-MARCO CE recipe.
    model_kwargs: dict[str, Any] = {"num_labels": 1}
    if max_seq_length and max_seq_length > 0:
        model_kwargs["max_length"] = max_seq_length

    model = CrossEncoder(base, **model_kwargs)

    examples = _build_examples(rows)
    loader = DataLoader(  # type: ignore[arg-type]
        examples,
        shuffle=True,
        batch_size=batch_size,
    )

    # CrossEncoder.fit expects a single train_dataloader (not the
    # SentenceTransformer multi-objective list). loss_fct=None makes it use
    # BCEWithLogitsLoss internally, which matches our ``label in {0, 1}`` data.
    fit_kwargs: dict[str, Any] = {
        "train_dataloader": loader,
        "epochs": epochs,
        "warmup_steps": max(10, steps // 10) if steps else 100,
        "optimizer_params": {"lr": learning_rate},
        "show_progress_bar": True,
    }
    # `--loss=lambda` is reserved for a future pairwise/listwise upgrade. We
    # emit a warning and fall through to BCE so a misconfigured CLI doesn't
    # silently break — the cross-encoder fit() only honours BCE today.
    if loss_name != "bce":
        print(
            f"[train_reranker] WARN loss={loss_name} is not implemented yet; falling back to BCE.",
            flush=True,
        )

    # `steps_per_epoch` only when the caller explicitly limited it (smoke
    # runs / kill gates). Full training lets the loader iterate the whole
    # dataset and relies on `epochs` for the schedule.
    if steps and steps > 0:
        fit_kwargs["steps_per_epoch"] = steps

    model.fit(**fit_kwargs)

    if kind == "hf":
        # CrossEncoder.save_to_hub may not exist on older ST versions;
        # fall back to model.save() + huggingface_hub.upload_folder for
        # cross-version compatibility. We try the native API first.
        if hasattr(model, "push_to_hub"):
            model.push_to_hub(target, private=True)
        elif hasattr(model, "save_to_hub"):
            model.save_to_hub(target, private=True)
        else:
            # Manual fallback: save to a temp dir, then upload via HfApi.
            import tempfile

            from huggingface_hub import HfApi

            with tempfile.TemporaryDirectory() as tmpd:
                model.save(tmpd)
                api = HfApi()
                api.create_repo(target, private=True, exist_ok=True)
                api.upload_folder(folder_path=tmpd, repo_id=target)
        print(f"[train_reranker] pushed to https://huggingface.co/{target}")
    else:
        out_path = Path(target)
        out_path.mkdir(parents=True, exist_ok=True)
        model.save(str(out_path))
        print(f"[train_reranker] saved to {out_path}")


# ----- CLI -------------------------------------------------------------------


def _build_argparser() -> argparse.ArgumentParser:
    """Build the argparse object. Factored out so unit tests can introspect."""
    p = argparse.ArgumentParser(description="Fine-tune a CrossEncoder reranker")
    p.add_argument("--base", default=DEFAULT_BASE)
    p.add_argument("--train", required=True, type=Path)
    p.add_argument("--steps", type=int, default=0)
    p.add_argument(
        "--out",
        required=True,
        help="Either hf:owner/name (push to Hub private) or local dir",
    )
    p.add_argument("--batch-size", type=int, default=16)
    p.add_argument("--lr", type=float, default=2e-5)
    p.add_argument("--epochs", type=int, default=1)
    p.add_argument(
        "--max-seq-length",
        type=int,
        default=0,
        help="Cap CE encoder context (0 = use model default).",
    )
    p.add_argument(
        "--loss",
        choices=LOSS_CHOICES,
        default=DEFAULT_LOSS,
        help="bce (default, BCEWithLogitsLoss) | lambda (placeholder, falls back to BCE today)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate args + load pairs but skip training",
    )
    p.add_argument(
        "--i-know-im-using-tiny-data",
        action="store_true",
        help=f"Override the MIN_TRAIN_ROWS={MIN_TRAIN_ROWS} guard. ONLY for smoke runs / unit tests.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    p = _build_argparser()
    args = p.parse_args(argv)

    if args.dry_run:
        rows = load_pairs(args.train)
        kind, target = _parse_out(args.out)
        print(
            f"DRY RUN: would train on {len(rows)} rows, base={args.base}, "
            f"steps={args.steps}, epochs={args.epochs}, "
            f"out_kind={kind}, target={target}, loss={args.loss}"
        )
        return 0

    train(
        args.base,
        args.train,
        args.steps,
        args.out,
        batch_size=args.batch_size,
        learning_rate=args.lr,
        epochs=args.epochs,
        max_seq_length=args.max_seq_length,
        loss_name=args.loss,
        allow_tiny_data=args.i_know_im_using_tiny_data,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
