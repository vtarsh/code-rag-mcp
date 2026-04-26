#!/usr/bin/env python3
"""Fine-tune nomic-embed-text-v1.5 on pay-com (query, doc) pairs.

Skeleton. Actual training runs on a RunPod pod (16 GB Mac can't fit a full step).

Usage on pod:
    python3 train_docs_embedder.py \
        --base=nomic-ai/nomic-embed-text-v1.5 \
        --train=/workspace/train_v0.jsonl \
        --steps=100 \
        --out=hf:vtarsh/pay-com-docs-embed-v0

JSONL row format depends on --loss:
    mnrl       (default): {"query": "...", "positive": "...", "negative"?: "..."}
    cosent              : {"query": "...", "positive": "...", "negative": "..."}
    marginmse           : {"query": "...", "positive": "...", "negative": "...", "margin": <float>}
    tsdae               : {"text": "..."}    (unsupervised, raw text only)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Final

DEFAULT_BASE: Final = "nomic-ai/nomic-embed-text-v1.5"
HF_OUT_PREFIX: Final = "hf:"

# Minimum row count for a *real* fine-tune. Any train file with fewer rows is
# almost certainly the legacy 10-pair `payfin-v0` dataset that produced the
# 2026-04-25 R1-stage2 -17.7pp regression — the agent had mined 2956 fresh
# triplets but `--train` silently fell back to a stale path. We refuse to start
# the optimizer in that case. Override only via --i-know-im-using-tiny-data
# (smoke runs / kill gates / unit tests). See
# `.claude/debug/r1_retry_prep_done.json` and `/tmp/phase2_r1_outcome.txt:80-83`
# for the full incident.
MIN_TRAIN_ROWS: Final = 1000

# nomic-embed-text-v1.5 requires these prefix tokens at both train + serve time.
# Must match src/models.py["docs"] query_prefix/document_prefix exactly —
# training unprefixed while serving prefixed silently collapses quality.
NOMIC_QUERY_PREFIX: Final = "search_query: "
NOMIC_DOCUMENT_PREFIX: Final = "search_document: "

# Loss families. Each family maps to a sentence-transformers losses.* class
# AND a different JSONL row shape — see _validate_rows_for_loss.
LOSS_CHOICES: Final = ("mnrl", "cosent", "marginmse", "tsdae")
DEFAULT_LOSS: Final = "mnrl"


def _parse_out(out: str) -> tuple[str, str]:
    """Return (kind, target). kind in {'hf', 'dir'}."""
    if out.startswith(HF_OUT_PREFIX):
        return "hf", out[len(HF_OUT_PREFIX) :]
    return "dir", out


def load_pairs(path: Path, apply_nomic_prefix: bool = True) -> list[dict]:
    """Load JSONL rows with {query, positive[, negative]}.

    When apply_nomic_prefix=True (default), wraps each query/positive with the
    nomic-embed-text-v1.5 prefix tokens so training distribution matches the
    prod serving path (src/models.py["docs"] + docs_vector_indexer._prepare_text).
    Set False (via --no-prefix) for bases that don't use these prefixes.
    """
    rows: list[dict] = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if "query" not in row or "positive" not in row:
                raise ValueError(f"Bad row (need query+positive): {row}")
            if apply_nomic_prefix:
                row["query"] = f"{NOMIC_QUERY_PREFIX}{row['query']}"
                row["positive"] = f"{NOMIC_DOCUMENT_PREFIX}{row['positive']}"
                if "negative" in row:
                    row["negative"] = f"{NOMIC_DOCUMENT_PREFIX}{row['negative']}"
            rows.append(row)
    return rows


def load_raw_text(path: Path) -> list[dict]:
    """Load JSONL rows for TSDAE (single column 'text', no labels).

    TSDAE is an *unsupervised* denoising autoencoder — it builds its own
    (corrupted, original) pairs internally via DenoisingAutoEncoderDataset,
    so rows must carry `text` (or already-loaded `positive` accepted as text).
    """
    rows: list[dict] = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            text = row.get("text")
            if text is None:
                # accept fallback to positive for convenience
                text = row.get("positive")
            if text is None:
                raise ValueError(f"Bad row for tsdae (need text): {row}")
            rows.append({"text": text})
    return rows


# ----- loss + data shape branching ------------------------------------------


def _validate_rows_for_loss(loss_name: str, rows: list[dict]) -> None:
    """Raise ValueError if `rows` don't carry the fields `loss_name` requires.

    Pure function — no model touched, safe to call before any heavy import.
    """
    if not rows:
        raise ValueError(f"loss={loss_name} got empty training set")

    if loss_name == "mnrl":
        # MNRL needs (q, pos); negatives are mined in-batch but optional
        # explicit negatives are also accepted.
        expected = {"query", "positive"}
    elif loss_name == "cosent":
        # CoSENT is a *pairwise margin* loss: it learns from (q, pos, label=1.0)
        # AND (q, neg, label=0.0) pairs. Needs explicit negatives.
        expected = {"query", "positive", "negative"}
    elif loss_name == "marginmse":
        # MarginMSE distills cross-encoder rank gaps. Triplet + teacher score.
        expected = {"query", "positive", "negative", "margin"}
    elif loss_name == "tsdae":
        # TSDAE is unsupervised — text only.
        expected = {"text"}
    else:
        raise ValueError(f"Unknown loss={loss_name!r}; expected one of {LOSS_CHOICES}")

    found = set(rows[0].keys())
    missing = expected - found
    if missing:
        raise ValueError(
            f"loss={loss_name} requires fields {sorted(expected)}; "
            f"JSONL had {sorted(found)} (missing: {sorted(missing)})"
        )


def _build_loss_and_data(loss_name: str, rows: list[dict], model: Any) -> tuple[Any, list[Any]]:
    """Return (loss_instance, examples) for the chosen loss family.

    Branches:
        mnrl       → MultipleNegativesRankingLoss + InputExample(texts=[q, pos])
        cosent     → CoSENTLoss + pairs (q, pos, 1.0) and (q, neg, 0.0)
        marginmse  → MarginMSELoss + triplets (q, pos, neg) labeled `margin`
        tsdae      → DenoisingAutoEncoderLoss + raw texts (single-col examples)

    `model` is forwarded to the loss class. Tests pass a fake/mock to avoid
    loading a 547 MB transformer, so we keep this function model-agnostic.
    """
    _validate_rows_for_loss(loss_name, rows)

    # Lazy import — keeps `import train_docs_embedder` cheap on Mac.
    from sentence_transformers import InputExample, losses

    if loss_name == "mnrl":
        examples = [InputExample(texts=[r["query"], r["positive"]]) for r in rows]
        loss = losses.MultipleNegativesRankingLoss(model)
        return loss, examples

    if loss_name == "cosent":
        examples = []
        for r in rows:
            examples.append(InputExample(texts=[r["query"], r["positive"]], label=1.0))
            examples.append(InputExample(texts=[r["query"], r["negative"]], label=0.0))
        loss = losses.CoSENTLoss(model)
        return loss, examples

    if loss_name == "marginmse":
        examples = [
            InputExample(
                texts=[r["query"], r["positive"], r["negative"]],
                label=float(r["margin"]),
            )
            for r in rows
        ]
        loss = losses.MarginMSELoss(model)
        return loss, examples

    if loss_name == "tsdae":
        # DenoisingAutoEncoderDataset wraps raw strings later in the
        # train() loop; here we just return the strings as InputExamples
        # with a single-text payload so DataLoader collates uniformly.
        examples = [InputExample(texts=[r["text"]]) for r in rows]
        loss = losses.DenoisingAutoEncoderLoss(model)
        return loss, examples

    raise ValueError(  # pragma: no cover — _validate_rows_for_loss already gated
        f"Unhandled loss={loss_name!r}"
    )


# ----- training entrypoint ---------------------------------------------------


def _copy_custom_modeling_files(base: str, out_dir: Path) -> None:
    """Copy *.py custom modeling files from base model's HF cache into out_dir.

    Bug 6p: nomic-bert (modeling_hf_nomic_bert.py) and Alibaba-NLP/new-impl
    (modeling.py) define their model classes in *.py files referenced by
    config.json's auto_map. SentenceTransformer.save() only writes weights +
    config + tokenizer — it does NOT copy these .py files. After uploading
    the saved dir to HF Hub, downstream `SentenceTransformer(repo,
    trust_remote_code=True)` finds no modeling.py at the new repo, falls
    back to standard BertModel architecture, and reports state_dict missing
    keys for `encoder.encoder.layers.X.*`.

    This helper downloads the base repo's *.py files via
    huggingface_hub.snapshot_download (allow_patterns=['*.py']) and copies
    them into the ST sub-module dir that holds config.json (typically
    `out_dir/0_<TransformerName>/`).
    """
    import shutil

    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        print("[train] WARN huggingface_hub missing — cannot copy *.py files", flush=True)
        return

    try:
        cache_dir = snapshot_download(repo_id=base, allow_patterns=["*.py"])
    except Exception as e:
        print(f"[train] WARN snapshot_download(base, *.py) failed: {e}", flush=True)
        return

    py_files = list(Path(cache_dir).glob("*.py"))
    if not py_files:
        return

    # ST stores the underlying transformer in a sub-dir like `0_Transformer/`,
    # `0_NewModel/`, etc. Find the one that holds config.json — that's where
    # auto_map lives and where modeling.py needs to be alongside.
    transformer_subdir = None
    for sub in sorted(out_dir.iterdir()):
        if sub.is_dir() and (sub / "config.json").exists():
            transformer_subdir = sub
            break
    if transformer_subdir is None:
        # Non-ST flat save — drop files at root
        transformer_subdir = out_dir

    copied = 0
    for py in py_files:
        dest = transformer_subdir / py.name
        if dest.exists():
            continue
        shutil.copy2(py, dest)
        copied += 1
    if copied:
        print(
            f"[train] copied {copied} custom modeling files "
            f"({', '.join(sorted(p.name for p in py_files))}) "
            f"into {transformer_subdir} (Bug 6p)",
            flush=True,
        )


def train(
    base: str,
    train_path: Path,
    steps: int,
    out: str,
    batch_size: int = 16,
    learning_rate: float = 2e-5,
    apply_nomic_prefix: bool = True,
    loss_name: str = DEFAULT_LOSS,
    epochs: int = 1,
    max_seq_length: int = 0,
    allow_tiny_data: bool = False,
) -> None:
    """Run the fine-tune. Heavy deps imported lazily so this file imports
    cleanly in tests on a Mac without the GPU stack.

    `epochs` and `max_seq_length` were added (R1 Stage B, 2026-04-25) so
    long-form training can run by epoch (not by step count) and so we can cap
    the encoder context window to fit a 24 GB GPU when the base default is
    huge (nomic-bert defaults to 8192 → activations OOM at bs=16).

    `allow_tiny_data` (R1-retry-prep, 2026-04-25) escapes the MIN_TRAIN_ROWS
    guard for kill-gate smoke runs / unit tests. **Never** set this in a
    production R1 run — the guard is the only thing standing between a
    miswired pod and a re-run of the -17.7pp R1-stage2 regression.
    """
    # HF_TOKEN check BEFORE heavy imports — a missing token on hf: output would
    # otherwise burn 10-20 min of GPU before the push_to_hub call raises.
    kind, target = _parse_out(out)
    if kind == "hf" and not os.getenv("HF_TOKEN"):
        raise RuntimeError("HF_TOKEN missing — required for hf: push")

    # Resolve to absolute path BEFORE any read so the log shows the real path
    # the optimizer will see. The 2026-04-25 R1-stage2 incident was directly
    # caused by `--train=/workspace/train_v0.jsonl` (stale 10-pair payfin-v0
    # dataset) silently substituting for the staged
    # `/workspace/train/r1_train_pairs.jsonl` (1130 mined pairs) — and the
    # absence of an absolute-path log meant the swap went unnoticed.
    abs_train_path = Path(train_path).resolve()
    print(f"[train] train_path_abs={abs_train_path!s}", flush=True)

    if loss_name == "tsdae":
        rows = load_raw_text(train_path)
    else:
        rows = load_pairs(train_path, apply_nomic_prefix=apply_nomic_prefix)

    # Visible row-count + first-3-row queries log: a human / pre-flight script
    # can spot a miswired training file in <1 s by comparing the printed `n_rows`
    # against the expected count from `r1_retry_prep_done.json`.
    n_rows = len(rows)
    print(f"[train] n_rows={n_rows} loaded from {abs_train_path}", flush=True)
    for i, row in enumerate(rows[:3]):
        sample_q = row.get("query") or row.get("text") or ""
        # Strip the nomic prefix only for display so the user sees the raw query
        if sample_q.startswith(NOMIC_QUERY_PREFIX):
            sample_q = sample_q[len(NOMIC_QUERY_PREFIX) :]
        # Cap visible length so a long doc body doesn't blow out the log.
        print(f"[train] row[{i}].query={sample_q[:120]!r}", flush=True)

    # Hard refuse to start the optimizer if the file looks like the legacy
    # 10-pair payfin-v0 stale dataset. The override (`allow_tiny_data=True`,
    # CLI `--i-know-im-using-tiny-data`) exists ONLY for smoke runs / unit
    # tests — production R1 runs must always carry ≥MIN_TRAIN_ROWS rows.
    if n_rows < MIN_TRAIN_ROWS and not allow_tiny_data:
        raise ValueError(
            f"REFUSE: training data {abs_train_path!s} has only {n_rows} rows "
            f"(need >= {MIN_TRAIN_ROWS}). This looks like the legacy 10-pair "
            f"payfin-v0 dataset that produced the 2026-04-25 R1-stage2 "
            f"-17.7pp regression. Check --train flag or pass "
            f"--i-know-im-using-tiny-data for smoke/unit runs."
        )

    print(
        f"[train] {len(rows)} rows; base={base}; steps={steps}; epochs={epochs}; "
        f"out={out}; bs={batch_size}; lr={learning_rate}; "
        f"nomic_prefix={apply_nomic_prefix}; loss={loss_name}; "
        f"max_seq_length={max_seq_length or 'default'}",
        flush=True,
    )

    from sentence_transformers import SentenceTransformer
    from torch.utils.data import DataLoader

    model = SentenceTransformer(base, trust_remote_code=True)
    # gte / Alibaba-NLP/new-impl ships `persistent=False` rotary + position_ids
    # buffers that transformers >= 5 + accelerate lazy-init silently drops post-
    # `from_pretrained`. Re-seed them here so encode()/fit() doesn't IndexError
    # on uninitialised int64 memory. No-op for non-new-impl models (mxbai/nomic/
    # bge-m3/etc) — the helper guards on
    # `type(embeddings).__name__ != 'NewEmbeddings'`.
    #
    # Inline copy of `src/index/builders/docs_vector_indexer.
    # _fix_gte_persistent_false_buffers` so this script runs on a RunPod pod
    # without requiring the `src/` tree to be present (the pod ships only this
    # standalone trainer + train data + eval).
    try:
        _auto = model._first_module().auto_model
        if type(_auto.embeddings).__name__ == "NewEmbeddings":
            import torch as _torch

            _cfg = _auto.config
            _auto.embeddings.register_buffer(
                "position_ids",
                _torch.arange(_cfg.max_position_embeddings, device=_auto.device),
                persistent=False,
            )
            _rot = _auto.embeddings.rotary_emb
            _inv_freq = 1.0 / (_rot.base ** (_torch.arange(0, _rot.dim, 2, device=_auto.device).float() / _rot.dim))
            if hasattr(_rot, "scaling_factor") and getattr(_rot, "mixed_b", None) is None:
                _inv_freq = _inv_freq / (_rot.scaling_factor ** (2 / _rot.dim))
            _rot.register_buffer("inv_freq", _inv_freq, persistent=False)
            _rot._set_cos_sin_cache(
                int(_rot.max_seq_len_cached),
                _inv_freq.device,
                _torch.float32,
            )
            print("[train] applied _fix_gte_persistent_false_buffers", flush=True)
        else:
            print(
                f"[train] _fix_gte_persistent_false_buffers no-op (embeddings={type(_auto.embeddings).__name__})",
                flush=True,
            )
    except Exception as e:  # pragma: no cover
        print(
            f"[train] WARN _fix_gte_persistent_false_buffers skipped: {e}",
            flush=True,
        )
    if max_seq_length and max_seq_length > 0:
        # Cap the encoder context window. Saves activations memory and keeps
        # the run inside a 24 GB GPU at typical training batch sizes.
        model.max_seq_length = max_seq_length
        try:
            # Some ST modules also expose a .max_seq_length on the underlying
            # Transformer — set it directly when accessible so the tokenizer
            # truncation matches the model's positional capacity.
            for module in model._modules.values():
                if hasattr(module, "max_seq_length"):
                    module.max_seq_length = max_seq_length
        except Exception:
            pass
        print(f"[train] capped max_seq_length={max_seq_length}")
    loss_fn, examples = _build_loss_and_data(loss_name, rows, model)
    loader = DataLoader(examples, shuffle=True, batch_size=batch_size)  # type: ignore[arg-type]

    # Bug 6o-debug: snapshot weight stats BEFORE fit so we can detect
    # mid-train corruption (gradient explosion) post-hoc. mxbai-large +
    # MNRL was observed to corrupt 389/391 params during fit().
    import torch as _torch

    pre_param = next(model.parameters())
    print(
        f"[train] PRE-FIT first_param shape={tuple(pre_param.shape)} "
        f"dtype={pre_param.dtype} mean={pre_param.mean().item():.6f} "
        f"std={pre_param.std().item():.6f} has_nan={bool(_torch.isnan(pre_param).any())}",
        flush=True,
    )
    pre_total = sum(1 for _ in model.parameters())
    print(f"[train] total parameter tensors: {pre_total}", flush=True)

    fit_kwargs: dict[str, Any] = {
        "train_objectives": [(loader, loss_fn)],
        "epochs": epochs,
        "warmup_steps": max(10, steps // 10) if steps else 100,
        "optimizer_params": {"lr": learning_rate},
        "show_progress_bar": True,
    }
    # Only pass `steps_per_epoch` when the caller explicitly limited it (smoke
    # runs / kill gates). For full training we let the loader iterate the
    # whole dataset and rely on `epochs` for the schedule.
    if steps and steps > 0:
        fit_kwargs["steps_per_epoch"] = steps

    model.fit(**fit_kwargs)

    # Bug 6o-debug: snapshot post-fit. Count NaN-bearing parameters; if any,
    # raise loudly so the orchestrator marks the candidate dead before HF push
    # and downstream waste.
    nan_param_names = [n for n, p in model.named_parameters() if _torch.isnan(p).any()]
    inf_param_names = [n for n, p in model.named_parameters() if _torch.isinf(p).any()]
    post_param = next(model.parameters())
    print(
        f"[train] POST-FIT first_param mean={post_param.mean().item():.6f} "
        f"std={post_param.std().item():.6f} has_nan={bool(_torch.isnan(post_param).any())}",
        flush=True,
    )
    if nan_param_names or inf_param_names:
        raise RuntimeError(
            f"FAST-FAIL post-fit: {len(nan_param_names)}/{pre_total} params "
            f"contain NaN, {len(inf_param_names)} contain Inf. First NaN: "
            f"{nan_param_names[:3]}. Train regime corrupts model — abort "
            f"before HF push wastes uploaded weights."
        )

    # Quick post-fit encode sanity probe (catches subtle pooling/norm bugs that
    # leave individual params clean but produce NaN downstream).
    try:
        probe_v = model.encode(["sanity probe text"])
        if _torch.from_numpy(probe_v if probe_v.ndim == 1 else probe_v[0]).isnan().any():
            raise RuntimeError("FAST-FAIL post-fit: model.encode produces NaN despite clean params.")
        print(f"[train] POST-FIT encode probe: shape={probe_v.shape} OK", flush=True)
    except Exception as e:
        if "FAST-FAIL" in str(e):
            raise
        print(f"[train] WARN post-fit probe encode raised: {e}", flush=True)

    if kind == "hf":
        model.push_to_hub(target, private=True)
        print(f"[train] pushed to https://huggingface.co/{target}")
    else:
        out_path = Path(target)
        out_path.mkdir(parents=True, exist_ok=True)
        model.save(str(out_path))
        print(f"[train] saved to {out_path}")
        # Bug 6p: SentenceTransformer.save() does NOT copy custom modeling files
        # (modeling_hf_nomic_bert.py for nomic, modeling.py for Alibaba-NLP/new-impl)
        # that the base model's config.json auto_map points to. A downstream
        # HF Hub upload of this dir + reload via
        # SentenceTransformer(repo, trust_remote_code=True) then falls back to
        # standard BertModel arch and the state_dict mismatches the FT'd weights.
        # Copy *.py files from the base model's HF cache so the saved repo is
        # self-contained.
        _copy_custom_modeling_files(base, out_path)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Fine-tune docs embedding model")
    p.add_argument("--base", default=DEFAULT_BASE)
    p.add_argument("--train", required=True, type=Path)
    p.add_argument("--steps", type=int, default=100)
    p.add_argument(
        "--out",
        required=True,
        help="Either hf:owner/name (push to Hub private) or local dir",
    )
    p.add_argument("--batch-size", type=int, default=16)
    p.add_argument("--lr", type=float, default=2e-5)
    p.add_argument(
        "--no-prefix",
        action="store_true",
        help="Skip nomic search_query:/search_document: prefixes "
        "(use for non-nomic bases whose serving path doesn't wrap text)",
    )
    p.add_argument(
        "--loss",
        choices=LOSS_CHOICES,
        default=DEFAULT_LOSS,
        help="Loss family. mnrl=MultipleNegativesRankingLoss (default, current), "
        "cosent=CoSENTLoss (pairwise margin), "
        "marginmse=MarginMSELoss (cross-encoder distillation, needs `margin`), "
        "tsdae=DenoisingAutoEncoderLoss (unsupervised, needs `text` only)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate args + load pairs but skip training",
    )
    p.add_argument(
        "--epochs",
        type=int,
        default=1,
        help="Epochs to train (default 1). Combined with --steps=0 means full pass.",
    )
    p.add_argument(
        "--max-seq-length",
        type=int,
        default=0,
        help="Cap encoder context (0 = use model default). nomic-bert default "
        "8192 OOMs at bs=16 on 24 GB; cap to 512 for safety.",
    )
    p.add_argument(
        "--i-know-im-using-tiny-data",
        action="store_true",
        help=f"Override the MIN_TRAIN_ROWS={MIN_TRAIN_ROWS} guard. ONLY for "
        "smoke runs / kill gates / unit tests. Production R1 must NEVER "
        "use this flag — see scripts/runpod/train_docs_embedder.py "
        "MIN_TRAIN_ROWS docstring for the incident this guard prevents.",
    )
    args = p.parse_args(argv)

    apply_prefix = not args.no_prefix

    if args.dry_run:
        if args.loss == "tsdae":
            rows = load_raw_text(args.train)
        else:
            rows = load_pairs(args.train, apply_nomic_prefix=apply_prefix)
        # Validate row shape against the chosen loss BEFORE pretending to train,
        # so a misconfigured run aborts on the Mac, not on a $0.34/h pod.
        _validate_rows_for_loss(args.loss, rows)
        kind, target = _parse_out(args.out)
        print(
            f"DRY RUN: would train on {len(rows)} rows, base={args.base}, "
            f"steps={args.steps}, out_kind={kind}, target={target}, "
            f"nomic_prefix={apply_prefix}, loss={args.loss}"
        )
        return 0

    train(
        args.base,
        args.train,
        args.steps,
        args.out,
        batch_size=args.batch_size,
        learning_rate=args.lr,
        apply_nomic_prefix=apply_prefix,
        loss_name=args.loss,
        epochs=args.epochs,
        max_seq_length=args.max_seq_length,
        allow_tiny_data=args.i_know_im_using_tiny_data,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
