"""Fine-tune CrossEncoder reranker on historical PI tasks (P5 pilot).

**Low-memory streaming implementation.** Two code paths:

Legacy (default, unchanged for v2 reproducibility):
  - `IterableDataset` streams JSONL line-by-line (no full list in memory).
  - Legacy `CrossEncoder.fit()` API (still present in sentence-transformers
    5.3) — plain DataLoader, no HF `Trainer` overhead.
  - `batch_size=4`, `max_length=256` — training comfortably under 3 GB RSS.

New optimised (enabled by --bf16 / --gradient-checkpointing / --fp16):
  - `sentence_transformers.cross_encoder.CrossEncoderTrainer` (HF Trainer
    subclass) with mixed precision + gradient checkpointing for larger
    models (e.g. `Alibaba-NLP/gte-reranker-modernbert-base`, 149M params).
  - Still uses memory-mapped `datasets.Dataset.from_json` so the 50k rows
    live on disk, not in RAM.
  - Added `MpsCacheHygieneCallback` to run `torch.mps.empty_cache()` +
    `gc.collect()` each step when MPS is the active device or grad_ckpt
    is on — reduces fragmentation on the 32GB M-series soft cap (~20GB).

Common to both:
  - `pause_daemon()` at start frees the daemon's own ~1 GB CrossEncoder +
    embed model so we don't overlap (same pattern as
    scripts/embed_missing_vectors.py).

Output: full model + tokenizer directory, loadable via `CrossEncoder(path)`.
"""

from __future__ import annotations

import argparse
import contextlib
import gc
import json
import logging
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
import warnings
from pathlib import Path

import torch
import torch.nn as nn
from sentence_transformers import CrossEncoder, InputExample
from torch.utils.data import DataLoader, IterableDataset, get_worker_info

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("finetune_reranker")

DAEMON_PORT = 8742

def pause_daemon(port: int = DAEMON_PORT, timeout: float = 5.0) -> bool:
    """Force-restart daemon (~1 GB resident models) to free RAM before training.

    Same /admin/shutdown endpoint used by scripts/embed_missing_vectors.py —
    drains in-flight then os._exit(0); launchd KeepAlive respawns fresh.
    /admin/unload alone drops refs but pymalloc doesn't release arena pages,
    so we need the full process restart to actually reclaim RSS.
    """
    url = f"http://127.0.0.1:{port}/admin/shutdown"
    req = urllib.request.Request(url, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read()
        log.info("daemon on :%d shutdown requested; launchd will restart fresh", port)
        return True
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", str(e))
        if isinstance(reason, OSError) and reason.errno in {61, 111}:
            return False
        log.info("daemon shutdown failed: %s; continuing", reason)
        return False
    except Exception as e:
        log.info("daemon shutdown error: %s; continuing", e)
        return False

class JsonlExampleStream(IterableDataset):
    """Yield `InputExample` rows from a JSONL file, line-by-line.

    Memory footprint: one parsed row at a time, regardless of file size.
    Supports shuffling via `shuffle_buffer` (reservoir buffer of N rows),
    still bounded — doesn't materialise the whole file.
    """

    def __init__(
        self,
        path: Path,
        *,
        max_doc_chars: int = 1500,
        shuffle_buffer: int = 0,
        seed: int = 42,
        skip_indices: set[int] | None = None,
        keep_indices: set[int] | None = None,
    ):
        self.path = path
        self.max_doc_chars = max_doc_chars
        self.shuffle_buffer = shuffle_buffer
        self.seed = seed
        self.skip_indices = skip_indices or set()
        self.keep_indices = keep_indices  # None = keep all (minus skip)

    def _raw_iter(self):
        with self.path.open("r", encoding="utf-8") as f:
            for idx, line in enumerate(f):
                line = line.strip()
                if not line:
                    continue
                if idx in self.skip_indices:
                    continue
                if self.keep_indices is not None and idx not in self.keep_indices:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                q = r.get("query")
                d = r.get("document")
                lbl = r.get("label")
                if q is None or d is None or lbl is None:
                    continue
                yield InputExample(
                    texts=[str(q), str(d)[: self.max_doc_chars]],
                    label=float(lbl),
                )

    def __iter__(self):
        info = get_worker_info()
        if info is not None and info.num_workers > 1:
            raw = (ex for i, ex in enumerate(self._raw_iter()) if i % info.num_workers == info.id)
        else:
            raw = self._raw_iter()

        if self.shuffle_buffer <= 1:
            yield from raw
            return

        rng = random.Random(self.seed)
        buf: list[InputExample] = []
        for ex in raw:
            if len(buf) < self.shuffle_buffer:
                buf.append(ex)
            else:
                j = rng.randrange(self.shuffle_buffer)
                yield buf[j]
                buf[j] = ex
        rng.shuffle(buf)
        yield from buf

def count_jsonl_rows(path: Path) -> int:
    n = 0
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                n += 1
    return n

def pick_device() -> str:
    if torch.backends.mps.is_available():
        return "mps"
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Fine-tune CrossEncoder reranker (streaming)")
    p.add_argument("--train", required=True)
    p.add_argument("--test", default=None, help="Test JSONL (reload smoke only)")
    p.add_argument("--base-model", default="cross-encoder/ms-marco-MiniLM-L-6-v2")
    p.add_argument("--out", required=True)
    p.add_argument("--epochs", type=int, default=2)
    p.add_argument("--batch-size", type=int, default=4)
    p.add_argument("--lr", type=float, default=2e-5)
    p.add_argument("--warmup", type=int, default=100)
    p.add_argument("--max-length", type=int, default=256)
    p.add_argument("--shuffle-buffer", type=int, default=512, help="Reservoir shuffle size. 0 = no shuffle.")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--val-ratio", type=float, default=0.10)
    p.add_argument("--no-pause-daemon", action="store_true")
    p.add_argument(
        "--loss",
        choices=["bce", "mse", "huber", "lambdaloss"],
        default="bce",
        help="Training loss. bce=default (sigmoid+BCE, can saturate); "
        "mse=regression on 0/1 labels (avoids score compression); "
        "huber=robust MSE; "
        "lambdaloss=listwise (requires input data in {query, docs, labels} "
        "format — see scripts/convert_to_listwise.py). Directly optimises "
        "NDCG, designed to fix rank-reshuffle regressions on multi-GT tickets.",
    )
    p.add_argument(
        "--bf16",
        action="store_true",
        help="Enable bfloat16 mixed precision (MPS/CUDA). "
        "Halves activation memory. Switches to CrossEncoderTrainer path.",
    )
    p.add_argument(
        "--fp16",
        action="store_true",
        help="Enable fp16 mixed precision (CUDA only — may be unstable on MPS). Switches to CrossEncoderTrainer path.",
    )
    p.add_argument(
        "--gradient-checkpointing",
        action="store_true",
        help="Re-compute activations in backward pass. -30-50%% peak memory. Switches to CrossEncoderTrainer path.",
    )
    p.add_argument(
        "--optim",
        choices=["adamw", "adamw_torch_fused", "adafactor", "sgd"],
        default="adamw",
        help="Optimizer. adamw(_torch_fused)=3x memory (default). adafactor=1.2x (slower convergence). sgd=1x.",
    )
    p.add_argument(
        "--attn-impl",
        choices=["sdpa", "eager", "flash_attention_2"],
        default="sdpa",
        help="Attention kernel. 'sdpa' is the PyTorch default; use 'eager' "
        "if ModernBERT+MPS+bf16 crashes on scaled_dot_product_attention.",
    )
    p.add_argument(
        "--save-steps",
        type=int,
        default=0,
        help="Save a checkpoint every N steps (new-trainer path only). "
        "0 = disabled. Keeps last 2 checkpoints to bound disk usage.",
    )
    p.add_argument(
        "--resume-from-checkpoint",
        type=str,
        default="",
        help="Path to a checkpoint dir to resume from (new-trainer path only). "
        "Pass 'none' to force fresh training even if checkpoints exist in --out. "
        "Empty (default) = auto-resume from latest checkpoint in --out if any.",
    )
    p.add_argument(
        "--early-stopping-patience",
        type=int,
        default=0,
        help="Enable EarlyStoppingCallback with this patience (val-loss plateau "
        "over N evaluations). 0 = disabled (default). Requires --save-steps>0 "
        "since eval_strategy is locked to save_strategy by HF Trainer.",
    )
    return p.parse_args()

def _latest_checkpoint(out_dir: Path) -> Path | None:
    """Find the highest-step ``checkpoint-*`` subdir under ``out_dir``.

    Returns None if ``out_dir`` doesn't exist or has no checkpoint subdirs.
    """
    if not out_dir.is_dir():
        return None
    best: tuple[int, Path] | None = None
    for child in out_dir.iterdir():
        if not child.is_dir():
            continue
        m = re.fullmatch(r"checkpoint-(\d+)", child.name)
        if not m:
            continue
        step = int(m.group(1))
        if best is None or step > best[0]:
            best = (step, child)
    return best[1] if best else None

def _use_new_trainer_path(args: argparse.Namespace) -> bool:
    """New HF Trainer path is enabled by any memory-opt flag."""
    return bool(args.bf16 or args.fp16 or args.gradient_checkpointing)

# --------------------------------------------------------------------------
# New CrossEncoderTrainer path (bf16 / fp16 / gradient_checkpointing)
# --------------------------------------------------------------------------

def detect_listwise_format(path: Path) -> bool:
    """Return True if the JSONL file has listwise rows (`docs` list).

    Peeks at the first non-empty JSON line. Pointwise files have `document`
    (str) + `label` (float). Listwise have `docs` (list[str]) + `labels`
    (list[float]). Used to dispatch training path and validate --loss.
    """
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "docs" in r and "labels" in r:
                return True
            if "document" in r and "label" in r:
                return False
            # Unknown schema — fall through to next line
    raise ValueError(f"could not detect data format from {path} (empty or malformed)")

def _build_hf_dataset(path: Path, keep_indices: set[int], *, max_doc_chars: int = 1500):
    """Load the kept rows of a pointwise JSONL file into a `datasets.Dataset`.

    We can't use `Dataset.from_json` directly (no row-filtering API), so we
    stream once, collect only the kept rows into plain Python dicts, then
    hand them to `Dataset.from_list`. For 45k rows with doc trimmed to
    1500 chars this is ~70MB in RAM — tolerable and still far below the
    legacy HF Trainer failure mode (full PyArrow materialisation + dtype
    metadata + Arrow cache).
    """
    from datasets import Dataset

    rows: list[dict] = []
    with path.open("r", encoding="utf-8") as f:
        for idx, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            if idx not in keep_indices:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            q = r.get("query")
            d = r.get("document")
            lbl = r.get("label")
            if q is None or d is None or lbl is None:
                continue
            rows.append(
                {
                    "query": str(q),
                    "document": str(d)[:max_doc_chars],
                    "label": float(lbl),
                }
            )
    return Dataset.from_list(rows)

def _build_hf_dataset_listwise(path: Path, keep_indices: set[int], *, max_doc_chars: int = 1500):
    """Load listwise rows ({query, docs, labels}) into a `datasets.Dataset`.

    LambdaLoss / ListNetLoss expect one row per group; the dataset columns
    are `query: str`, `docs: List[str]`, `labels: List[float]`. Each doc
    within a group is truncated to `max_doc_chars` to bound tokenisation
    work per forward pass (entire group processes together).
    """
    from datasets import Dataset

    rows: list[dict] = []
    with path.open("r", encoding="utf-8") as f:
        for idx, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            if idx not in keep_indices:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            q = r.get("query")
            docs = r.get("docs")
            labels = r.get("labels")
            if q is None or not isinstance(docs, list) or not isinstance(labels, list):
                continue
            if len(docs) != len(labels) or not docs:
                continue
            rows.append(
                {
                    "query": str(q),
                    "docs": [str(d)[:max_doc_chars] for d in docs],
                    "labels": [float(lbl) for lbl in labels],
                }
            )
    return Dataset.from_list(rows)

class _MpsCacheHygieneCallback:
    """HF Trainer callback: flush MPS cache + gc every N steps.

    Real class is defined inside `_run_new_trainer` to avoid a top-level
    import of `transformers.TrainerCallback` (keeps the legacy path import
    surface minimal).
    """

def _run_new_trainer(
    args: argparse.Namespace,
    *,
    train_path: Path,
    train_idx: set[int],
    val_idx: set[int],
    device: str,
    out_dir: Path,
    resume_path: str | None,
) -> tuple[CrossEncoder, float, float | None, str]:
    """Train via CrossEncoderTrainer (bf16 / grad_ckpt / custom optim).

    Returns (model, duration_seconds, final_val_loss, resumed_from_checkpoint).
    ``resumed_from_checkpoint`` is the resolved path string (empty if fresh).
    """
    from sentence_transformers.cross_encoder import (
        CrossEncoderTrainer,
        CrossEncoderTrainingArguments,
    )
    from sentence_transformers.cross_encoder.losses import (
        BinaryCrossEntropyLoss,
        LambdaLoss,
        MSELoss,
    )
    from transformers import TrainerCallback

    class MpsCacheHygieneCallback(TrainerCallback):
        """Flush MPS cache + python gc after each step.

        Only does work when device is MPS OR grad_ckpt is on, since the
        hygiene is cheap but not free.
        """

        def __init__(self, active: bool):
            self.active = active

        def _flush(self) -> None:
            if not self.active:
                return
            if torch.backends.mps.is_available():
                with contextlib.suppress(Exception):
                    torch.mps.empty_cache()
            gc.collect()

        def on_substep_end(self, cb_args, state, control, **kwargs):
            # Fires between forward and optimizer step when grad accumulation
            # is on, AND also once per step at the end of backward. Cleans
            # mid-step MPS fragmentation that caused OOM during backward().
            self._flush()
            return control

        def on_step_end(self, cb_args, state, control, **kwargs):
            self._flush()
            return control

    # --- bf16/fp16 device guard ---
    bf16 = args.bf16
    fp16 = args.fp16
    if bf16 and device not in {"mps", "cuda"}:
        log.warning("bf16 requested but device=%s (not mps/cuda). Falling back to fp32.", device)
        bf16 = False
    if fp16:
        if device != "cuda":
            log.warning(
                "fp16 requested but device=%s. fp16 is known to be unstable on MPS — "
                "ignoring fp16 flag. Use --bf16 instead on MPS.",
                device,
            )
            fp16 = False
        else:
            log.warning(
                "fp16 enabled. Some MPS ops have numerical issues with fp16 — use --bf16 on MPS if you hit NaNs."
            )

    # --- load model with optional attn impl ---
    log.info(
        "loading base model (new trainer path): %s (attn_impl=%s)",
        args.base_model,
        args.attn_impl,
    )
    model_kwargs: dict = {}
    if args.attn_impl and args.attn_impl != "sdpa":
        # sdpa is the HF default; explicit 'eager' is useful when MPS bf16
        # crashes scaled_dot_product_attention on ModernBERT.
        model_kwargs["attn_implementation"] = args.attn_impl
    if bf16:
        # Load weights in bf16 directly to match AMP activations.
        model_kwargs["torch_dtype"] = torch.bfloat16

    try:
        model = CrossEncoder(
            args.base_model,
            num_labels=1,
            max_length=args.max_length,
            device=device,
            trust_remote_code=True,
            model_kwargs=model_kwargs,
            tokenizer_kwargs={"truncation": True},
        )
    except Exception as e:
        if args.attn_impl == "sdpa":
            log.warning("CrossEncoder load failed with attn_impl=sdpa (%s); retrying with eager", e)
            model_kwargs["attn_implementation"] = "eager"
            model = CrossEncoder(
                args.base_model,
                num_labels=1,
                max_length=args.max_length,
                device=device,
                trust_remote_code=True,
                model_kwargs=model_kwargs,
                tokenizer_kwargs={"truncation": True},
            )
        else:
            raise

    # --- build datasets (memory-bounded: filter while reading) ---
    log.info("building HF datasets from JSONL (train=%d val=%d)", len(train_idx), len(val_idx))
    is_listwise = args.loss == "lambdaloss"
    if is_listwise:
        train_ds = _build_hf_dataset_listwise(train_path, train_idx)
        val_ds = _build_hf_dataset_listwise(train_path, val_idx)
    else:
        train_ds = _build_hf_dataset(train_path, train_idx)
        val_ds = _build_hf_dataset(train_path, val_idx)

    # --- pick loss (CrossEncoderTrainer losses, not bare nn modules) ---
    if args.loss == "mse":
        loss_mod = MSELoss(model)
    elif args.loss == "bce":
        loss_mod = BinaryCrossEntropyLoss(model)
    elif args.loss == "huber":
        # No dedicated CE HuberLoss — wrap MSELoss by swapping the torch loss.
        loss_mod = MSELoss(model)
        loss_mod.loss_fct = nn.HuberLoss()
    elif args.loss == "lambdaloss":
        # Listwise: optimises NDCG directly. Targets the rank-reshuffle
        # regressions that dominate v6.2 failures (92.5% per audit).
        loss_mod = LambdaLoss(model)
    else:
        raise ValueError(f"unknown loss: {args.loss}")
    log.info("loss: %s (%s)", args.loss, type(loss_mod).__name__)

    # --- optimizer ---
    optim_map = {
        "adamw": "adamw_torch",
        "adamw_torch_fused": "adamw_torch_fused",
        "adafactor": "adafactor",
        "sgd": "sgd",
    }
    optim_name = optim_map[args.optim]

    steps_per_epoch = max(1, (len(train_idx) + args.batch_size - 1) // args.batch_size)
    total_steps = steps_per_epoch * args.epochs
    log.info(
        "training (new trainer): epochs=%d batch=%d steps/epoch=%d total=%d "
        "warmup=%d lr=%g max_len=%d bf16=%s fp16=%s grad_ckpt=%s optim=%s",
        args.epochs,
        args.batch_size,
        steps_per_epoch,
        total_steps,
        args.warmup,
        args.lr,
        args.max_length,
        bf16,
        fp16,
        args.gradient_checkpointing,
        optim_name,
    )

    # Checkpointing: only enabled when --save-steps > 0. Keep last 2 to
    # bound disk growth (final model is saved separately via model.save()).
    if args.save_steps and args.save_steps > 0:
        save_strategy = "steps"
        save_steps = int(args.save_steps)
        save_total_limit = 2
        log.info("checkpointing: every %d steps, keeping last %d", save_steps, save_total_limit)
    else:
        save_strategy = "no"
        save_steps = 0
        save_total_limit = None

    train_args = CrossEncoderTrainingArguments(
        output_dir=str(out_dir),
        num_train_epochs=args.epochs,
        per_device_train_batch_size=args.batch_size,
        per_device_eval_batch_size=args.batch_size,
        learning_rate=args.lr,
        warmup_steps=args.warmup,
        bf16=bf16,
        fp16=fp16,
        gradient_checkpointing=args.gradient_checkpointing,
        optim=optim_name,
        seed=args.seed,
        data_seed=args.seed,
        save_strategy=save_strategy,
        save_steps=save_steps if save_steps else 500,  # HF requires >0 even when unused
        save_total_limit=save_total_limit,
        eval_strategy=save_strategy if args.early_stopping_patience > 0 else "no",
        eval_steps=save_steps if save_steps else 500,
        load_best_model_at_end=args.early_stopping_patience > 0,
        metric_for_best_model="eval_loss" if args.early_stopping_patience > 0 else None,
        greater_is_better=False if args.early_stopping_patience > 0 else None,
        logging_strategy="steps",
        logging_steps=max(1, steps_per_epoch // 10),
        report_to=[],
        dataloader_num_workers=0,
        remove_unused_columns=False,
        disable_tqdm=False,
    )

    from transformers import TrainerCallback

    hygiene_active = args.gradient_checkpointing or torch.backends.mps.is_available()
    callbacks: list[TrainerCallback] = [MpsCacheHygieneCallback(active=hygiene_active)]
    if args.early_stopping_patience > 0:
        from transformers import EarlyStoppingCallback

        callbacks.append(
            EarlyStoppingCallback(
                early_stopping_patience=args.early_stopping_patience,
            )
        )
        log.info("early-stopping enabled: patience=%d evals on eval_loss", args.early_stopping_patience)

    trainer = CrossEncoderTrainer(
        model=model,
        args=train_args,
        train_dataset=train_ds,
        eval_dataset=val_ds,
        loss=loss_mod,
        callbacks=callbacks,
    )

    resumed_from = ""
    t0 = time.time()
    if resume_path:
        log.info("resuming from %s", resume_path)
        trainer.train(resume_from_checkpoint=resume_path)
        resumed_from = resume_path
    else:
        trainer.train()
    dur = time.time() - t0
    log.info("training done in %.1fs (%.1f min)", dur, dur / 60.0)

    # --- final val loss: reuse the same loss module with a manual eval pass ---
    final_val_loss: float | None = None
    if is_listwise:
        # For listwise, val-loss comparison across loss types isn't
        # apples-to-apples; we report the trainer's own last evaluation
        # (or simply skip). Manual NDCG replication here adds complexity
        # for little gain — the real verdict comes from eval_finetune.py.
        log.info("skipping manual val-loss computation (listwise training)")
    else:
        with contextlib.suppress(Exception):
            model.model.eval()
            total_loss, n = 0.0, 0
            with torch.no_grad():
                for start in range(0, len(val_ds), args.batch_size):
                    chunk = val_ds[start : start + args.batch_size]
                    q = chunk["query"]
                    d = chunk["document"]
                    labels = torch.tensor(chunk["label"], dtype=torch.float32, device=device)
                    # Mimic what CE losses do internally (tokenize + forward).
                    tokens = model.tokenizer(
                        list(zip(q, d, strict=False)),
                        padding=True,
                        truncation=True,
                        return_tensors="pt",
                    )
                    tokens = {k: v.to(device) for k, v in tokens.items()}
                    logits = model.model(**tokens).logits.view(-1)
                    if args.loss == "bce":
                        loss = nn.BCEWithLogitsLoss()(logits.float(), labels)
                    elif args.loss == "mse":
                        loss = nn.MSELoss()(logits.float(), labels)
                    else:  # huber
                        loss = nn.HuberLoss()(logits.float(), labels)
                    batch_n = len(q)
                    total_loss += loss.item() * batch_n
                    n += batch_n
            if n:
                final_val_loss = total_loss / n
                log.info("final val loss (%s): %.4f (n=%d)", args.loss, final_val_loss, n)

    return model, dur, final_val_loss, resumed_from

# --------------------------------------------------------------------------
# Legacy model.fit() path — unchanged behaviour
# --------------------------------------------------------------------------

def _run_legacy_fit(
    args: argparse.Namespace,
    *,
    train_path: Path,
    train_idx: set[int],
    val_idx: set[int],
    device: str,
    out_dir: Path,
) -> tuple[CrossEncoder, float, float | None]:
    """Original streaming `model.fit()` training (v2 reproducible)."""
    train_stream = JsonlExampleStream(
        train_path,
        max_doc_chars=1500,
        shuffle_buffer=args.shuffle_buffer,
        seed=args.seed,
        keep_indices=train_idx,
    )

    log.info("loading base model: %s", args.base_model)
    model = CrossEncoder(
        args.base_model,
        num_labels=1,
        max_length=args.max_length,
        device=device,
    )

    train_loader = DataLoader(
        train_stream,
        batch_size=args.batch_size,
        num_workers=0,
        collate_fn=model.smart_batching_collate,
    )

    steps_per_epoch = (len(train_idx) + args.batch_size - 1) // args.batch_size
    total_steps = steps_per_epoch * args.epochs
    log.info(
        "training (legacy fit): epochs=%d batch=%d steps/epoch=%d total=%d warmup=%d lr=%g max_len=%d",
        args.epochs,
        args.batch_size,
        steps_per_epoch,
        total_steps,
        args.warmup,
        args.lr,
        args.max_length,
    )

    loss_map = {
        "bce": nn.BCEWithLogitsLoss(),
        "mse": nn.MSELoss(),
        "huber": nn.HuberLoss(),
    }
    loss_fct = loss_map[args.loss]
    log.info("loss: %s (%s)", args.loss, type(loss_fct).__name__)

    t0 = time.time()
    model.fit(
        train_dataloader=train_loader,
        loss_fct=loss_fct,
        epochs=args.epochs,
        warmup_steps=args.warmup,
        optimizer_params={"lr": args.lr},
        output_path=str(out_dir),
        save_best_model=False,
        show_progress_bar=True,
        use_amp=False,
    )
    dur = time.time() - t0
    log.info("training done in %.1fs (%.1f min)", dur, dur / 60.0)

    final_val_loss: float | None = None
    with contextlib.suppress(Exception):
        model.model.eval()

        val_stream = JsonlExampleStream(
            train_path,
            max_doc_chars=1500,
            shuffle_buffer=0,
            seed=args.seed,
            keep_indices=val_idx,
        )
        total_loss, n = 0.0, 0
        with torch.no_grad():
            batch: list[InputExample] = []
            for ex in val_stream:
                batch.append(ex)
                if len(batch) >= args.batch_size:
                    features, labels = model.smart_batching_collate(batch)
                    features = {k: v.to(device) for k, v in features.items()}
                    labels = labels.to(device).float()
                    logits = model.model(**features).logits.view(-1)
                    loss = loss_fct(logits, labels)
                    total_loss += loss.item() * len(batch)
                    n += len(batch)
                    batch = []
            if batch:
                features, labels = model.smart_batching_collate(batch)
                features = {k: v.to(device) for k, v in features.items()}
                labels = labels.to(device).float()
                logits = model.model(**features).logits.view(-1)
                loss = loss_fct(logits, labels)
                total_loss += loss.item() * len(batch)
                n += len(batch)
        if n:
            final_val_loss = total_loss / n
            log.info("final val loss (%s): %.4f (n=%d)", args.loss, final_val_loss, n)

    return model, dur, final_val_loss

def main() -> int:
    args = parse_args()

    warnings.filterwarnings("ignore", message="Token indices sequence length is longer than the specified maximum")
    logging.getLogger("transformers.tokenization_utils_base").setLevel(logging.ERROR)

    random.seed(args.seed)
    torch.manual_seed(args.seed)

    train_path = Path(args.train)
    if not train_path.is_file():
        log.error("train file not found: %s", train_path)
        return 2
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    if not args.no_pause_daemon:
        pause_daemon()

    total_rows = count_jsonl_rows(train_path)
    if total_rows == 0:
        log.error("empty train file")
        return 2
    rng = random.Random(args.seed)
    all_idx = list(range(total_rows))
    rng.shuffle(all_idx)
    n_val = max(1, round(total_rows * args.val_ratio))
    val_idx = set(all_idx[:n_val])
    train_idx = set(all_idx[n_val:])
    log.info("split indices: train=%d val=%d (val_ratio=%.2f)", len(train_idx), len(val_idx), args.val_ratio)

    device = pick_device()
    log.info("device: %s", device)

    use_new = _use_new_trainer_path(args)
    if args.loss == "lambdaloss":
        if not use_new:
            log.warning(
                "--loss=lambdaloss requires the new trainer path — auto-enabling bf16. "
                "Pass --fp16 or --gradient-checkpointing explicitly if you want those instead."
            )
            args.bf16 = True
            use_new = True
        try:
            is_listwise_data = detect_listwise_format(train_path)
        except ValueError as e:
            log.error("cannot detect train data format: %s", e)
            return 2
        if not is_listwise_data:
            log.error(
                "--loss=lambdaloss needs listwise data ({query, docs, labels}). "
                "Got pointwise data. Run scripts/convert_to_listwise.py first."
            )
            return 2
    log.info("training path: %s", "new (CrossEncoderTrainer)" if use_new else "legacy (CrossEncoder.fit)")

    mps_watermark_ratio = os.environ.get("PYTORCH_MPS_HIGH_WATERMARK_RATIO", "")
    if use_new and not mps_watermark_ratio:
        log.warning(
            "PYTORCH_MPS_HIGH_WATERMARK_RATIO not set; defaulting to 0.7 to cap "
            "MPS allocations (~11GB on a 16GB Mac). Set the env var explicitly to override."
        )
        os.environ["PYTORCH_MPS_HIGH_WATERMARK_RATIO"] = "0.7"
        os.environ.setdefault("PYTORCH_MPS_LOW_WATERMARK_RATIO", "0.5")
        mps_watermark_ratio = "0.7"

    resume_path = ""
    if use_new:
        req = (args.resume_from_checkpoint or "").strip()
        if req.lower() == "none":
            log.info("auto-resume disabled by --resume-from-checkpoint=none")
        elif req:
            if Path(req).is_dir():
                resume_path = req
            else:
                log.warning("--resume-from-checkpoint=%s does not exist; starting fresh", req)
        else:
            latest = _latest_checkpoint(out_dir)
            if latest is not None:
                log.info("auto-resuming from %s (pass --resume-from-checkpoint=none to disable)", latest)
                resume_path = str(latest)
    else:
        if args.save_steps or args.resume_from_checkpoint:
            log.warning(
                "--save-steps / --resume-from-checkpoint have no effect in the legacy "
                "CrossEncoder.fit() path; ignoring. Enable --bf16 / --gradient-checkpointing "
                "/ --fp16 to use the new trainer path with checkpointing."
            )

    if use_new:
        model, dur, final_val_loss, resumed_from = _run_new_trainer(
            args,
            train_path=train_path,
            train_idx=train_idx,
            val_idx=val_idx,
            device=device,
            out_dir=out_dir,
            resume_path=resume_path or None,
        )
    else:
        model, dur, final_val_loss = _run_legacy_fit(
            args,
            train_path=train_path,
            train_idx=train_idx,
            val_idx=val_idx,
            device=device,
            out_dir=out_dir,
        )
        resumed_from = ""

    model.save(str(out_dir))

    summary = {
        "base_model": args.base_model,
        "train_file": str(train_path),
        "test_file": args.test,
        "train_examples": len(train_idx),
        "val_examples": len(val_idx),
        "epochs": args.epochs,
        "batch_size": args.batch_size,
        "lr": args.lr,
        "warmup_steps": args.warmup,
        "max_length": args.max_length,
        "shuffle_buffer": args.shuffle_buffer,
        "seed": args.seed,
        "loss": args.loss,
        "device": device,
        "duration_seconds": round(dur, 2),
        "final_val_loss": final_val_loss,
        "training_path": "new" if use_new else "legacy",
        "bf16": bool(args.bf16),
        "fp16": bool(args.fp16),
        "gradient_checkpointing": bool(args.gradient_checkpointing),
        "optim": args.optim,
        "attn_impl": args.attn_impl if use_new else None,
        "save_steps": int(args.save_steps or 0),
        "resumed_from_checkpoint": resumed_from,
        "mps_watermark_ratio": os.environ.get("PYTORCH_MPS_HIGH_WATERMARK_RATIO", ""),
    }
    (out_dir / "training_summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    log.info("reload smoke: CrossEncoder(%s)", out_dir)
    reloaded = CrossEncoder(str(out_dir), device=device, trust_remote_code=True)
    score = reloaded.predict([("test query", "def test(): pass")])
    score_val = float(score[0]) if hasattr(score, "__len__") else float(score)
    log.info("reload smoke score: %s", score_val)

    print(
        json.dumps(
            {
                "status": "ok",
                "out_dir": str(out_dir),
                "duration_seconds": round(dur, 2),
                "final_val_loss": final_val_loss,
                "reload_smoke_score": score_val,
            },
            indent=2,
        )
    )
    return 0

if __name__ == "__main__":
    sys.exit(main())
