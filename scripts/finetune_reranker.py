"""Fine-tune CrossEncoder reranker on historical PI tasks (P5 pilot).

**Low-memory streaming implementation.** Previous version used HF `Trainer`
+ `datasets.Dataset.from_list()` which materialises the whole train set in
PyArrow and ballooned RSS to 10+ GB on M1 Pro with just 22M-param MiniLM.

This version:
  - `IterableDataset` streams JSONL line-by-line (no full list in memory).
  - Legacy `CrossEncoder.fit()` API (still present in sentence-transformers
    5.3) — plain DataLoader, no HF `Trainer` overhead.
  - `batch_size=4`, `max_length=256` — training comfortably under 3 GB RSS.
  - `pause_daemon()` at start frees the daemon's own ~1 GB CrossEncoder +
    embed model so we don't overlap (same pattern as
    scripts/embed_missing_vectors.py).

Output: full model + tokenizer directory, loadable via `CrossEncoder(path)`.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import random
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import torch
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
    """Release daemon's resident models (~1 GB) to free RAM before training.

    Same /admin/unload endpoint used by scripts/embed_missing_vectors.py.
    Daemon exits, launchd restarts it fresh; training sees a clean ~1 GB
    free vs shared RAM.
    """
    url = f"http://127.0.0.1:{port}/admin/unload"
    req = urllib.request.Request(url, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read()
        log.info("daemon on :%d unloaded + exiting for launchd restart", port)
        return True
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", str(e))
        if isinstance(reason, OSError) and reason.errno in {61, 111}:
            return False
        log.info("daemon unload failed: %s; continuing", reason)
        return False
    except Exception as e:
        log.info("daemon unload error: %s; continuing", e)
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
        # Single-worker streaming (MPS doesn't benefit from multiworker).
        info = get_worker_info()
        if info is not None and info.num_workers > 1:
            # Shard across workers by modulo
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
    p.add_argument("--shuffle-buffer", type=int, default=512,
                   help="Reservoir shuffle size. 0 = no shuffle.")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--val-ratio", type=float, default=0.10)
    p.add_argument("--no-pause-daemon", action="store_true")
    return p.parse_args()


def main() -> int:
    args = parse_args()

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

    # Precompute train/val split indices without materialising the file.
    total_rows = count_jsonl_rows(train_path)
    if total_rows == 0:
        log.error("empty train file")
        return 2
    rng = random.Random(args.seed)
    all_idx = list(range(total_rows))
    rng.shuffle(all_idx)
    n_val = max(1, int(round(total_rows * args.val_ratio)))
    val_idx = set(all_idx[:n_val])
    train_idx = set(all_idx[n_val:])
    log.info("split indices: train=%d val=%d (val_ratio=%.2f)",
             len(train_idx), len(val_idx), args.val_ratio)

    train_stream = JsonlExampleStream(
        train_path,
        max_doc_chars=1500,
        shuffle_buffer=args.shuffle_buffer,
        seed=args.seed,
        keep_indices=train_idx,
    )

    device = pick_device()
    log.info("device: %s", device)

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

    # steps/epoch = ceil(train_rows / batch)
    steps_per_epoch = (len(train_idx) + args.batch_size - 1) // args.batch_size
    total_steps = steps_per_epoch * args.epochs
    log.info(
        "training: epochs=%d batch=%d steps/epoch=%d total=%d warmup=%d lr=%g max_len=%d",
        args.epochs, args.batch_size, steps_per_epoch, total_steps,
        args.warmup, args.lr, args.max_length,
    )

    t0 = time.time()
    model.fit(
        train_dataloader=train_loader,
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

    # Streaming val loss — same anti-RAM pattern
    final_val_loss: float | None = None
    with contextlib.suppress(Exception):
        model.model.eval()
        import torch.nn.functional as F

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
                    loss = F.binary_cross_entropy_with_logits(logits, labels)
                    total_loss += loss.item() * len(batch)
                    n += len(batch)
                    batch = []
            if batch:
                features, labels = model.smart_batching_collate(batch)
                features = {k: v.to(device) for k, v in features.items()}
                labels = labels.to(device).float()
                logits = model.model(**features).logits.view(-1)
                loss = F.binary_cross_entropy_with_logits(logits, labels)
                total_loss += loss.item() * len(batch)
                n += len(batch)
        if n:
            final_val_loss = total_loss / n
            log.info("final val loss: %.4f (n=%d)", final_val_loss, n)

    # Ensure tokenizer is saved alongside model for reload compat
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
        "device": device,
        "duration_seconds": round(dur, 2),
        "final_val_loss": final_val_loss,
    }
    (out_dir / "training_summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    # Reload smoke
    log.info("reload smoke: CrossEncoder(%s)", out_dir)
    reloaded = CrossEncoder(str(out_dir), device=device)
    score = reloaded.predict([("test query", "def test(): pass")])
    score_val = float(score[0]) if hasattr(score, "__len__") else float(score)
    log.info("reload smoke score: %s", score_val)

    print(json.dumps({
        "status": "ok",
        "out_dir": str(out_dir),
        "duration_seconds": round(dur, 2),
        "final_val_loss": final_val_loss,
        "reload_smoke_score": score_val,
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
