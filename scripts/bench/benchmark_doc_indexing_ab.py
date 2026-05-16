#!/usr/bin/env python3
"""Sequential A/B builds of candidate docs-tower embedding models.

Builds three LanceDB tables (one per candidate) sharing the same
post-chunker-fix knowledge.db corpus. SEQUENTIAL — never two model loads at
once (lesson from 2026-04-23: 4× concurrent build_docs_vectors = 21 GB virt
on 16 GB Mac → freeze).

Candidates (from project_docs_model_research_2026_04_24.md shortlist):
    docs-gte-large    Alibaba-NLP/gte-large-en-v1.5      1024d  no prefix
    docs-arctic-l-v2  Snowflake/snowflake-arctic-embed-l 1024d  query: prefix
    docs-bge-m3-dense BAAI/bge-m3 (dense only)           1024d  no prefix

For each candidate:
    1. Pre-flight: sys-avail >= 5 GB hard, >= 3 GB warn. Abort if violated.
    2. Pre-flight: pgrep -f build_vectors|full_update returns nothing else.
    3. Drop checkpoint (per-model file) to force a clean run.
    4. Call build_docs_vectors(..., model_key=KEY, force=True).
    5. Free model + MPS cache; sleep 60s for kernel pages to release.
    6. Manifest: bench_runs/build_<key>_<ts>.json with knowledge_db_md5,
       chunks_embedded, lance_dir size, build_seconds.

Reranker stays reranker_ft_gte_v8. Code tower (CodeRankEmbed) untouched.

Usage:
    python3 scripts/benchmark_doc_indexing_ab.py
    python3 scripts/benchmark_doc_indexing_ab.py --skip docs-bge-m3-dense
    python3 scripts/benchmark_doc_indexing_ab.py --only docs-gte-large
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import gc
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(os.getenv("CODE_RAG_HOME", str(Path.home() / ".code-rag-mcp")))
sys.path.insert(0, str(ROOT))
from scripts._common import setup_paths

setup_paths()

DB_PATH = ROOT / "db" / "knowledge.db"
BENCH_DIR = ROOT / "bench_runs"

# Pre-flight thresholds (GB).
PREFLIGHT_AVAIL_HARD_GB = 5.0
PREFLIGHT_AVAIL_WARN_GB = 6.0
BETWEEN_BUILDS_SLEEP_S = 60

CANDIDATES: tuple[str, ...] = (
    "docs-gte-large",
    "docs-arctic-l-v2",
    "docs-bge-m3-dense",
)


def _md5_file(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _avail_gb() -> float:
    import psutil

    return psutil.virtual_memory().available / 1024**3


def _check_no_concurrent_compute(self_pid: int) -> list[str]:
    """Return command-lines of any other compute processes still running.

    Empty list = green. We exclude this script's own PID + its parent shell.
    """
    try:
        out = subprocess.check_output(
            ["pgrep", "-fl", r"build_vectors|build_docs_vectors|full_update"],
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except subprocess.CalledProcessError:
        return []
    rows = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            pid_str, cmd = line.split(None, 1)
            pid = int(pid_str)
        except (ValueError, IndexError):
            continue
        if pid == self_pid or pid == os.getppid():
            continue
        # Skip ourselves by script-name match (parent zsh / bash -c chains).
        if "benchmark_doc_indexing_ab.py" in cmd:
            continue
        rows.append(f"pid={pid} {cmd}")
    return rows


def preflight() -> dict:
    """Return a manifest dict on success; sys.exit(2) on hard fail."""
    if not DB_PATH.exists():
        print(f"ABORT: knowledge.db missing at {DB_PATH}", file=sys.stderr)
        sys.exit(2)

    avail = _avail_gb()
    if avail < PREFLIGHT_AVAIL_HARD_GB:
        print(
            f"ABORT: sys-avail={avail:.2f}G < {PREFLIGHT_AVAIL_HARD_GB}G hard threshold",
            file=sys.stderr,
        )
        sys.exit(2)
    if avail < PREFLIGHT_AVAIL_WARN_GB:
        print(f"WARN: sys-avail={avail:.2f}G below {PREFLIGHT_AVAIL_WARN_GB}G warn band")

    blockers = _check_no_concurrent_compute(os.getpid())
    if blockers:
        print("ABORT: other compute processes detected:", file=sys.stderr)
        for b in blockers:
            print(f"  {b}", file=sys.stderr)
        sys.exit(2)

    md5 = _md5_file(DB_PATH)
    print(f"  knowledge.db md5 = {md5}")
    print(f"  sys-avail        = {avail:.2f}G")
    return {"knowledge_db_md5": md5, "preflight_avail_gb": round(avail, 2)}


def build_one(key: str, common: dict) -> dict:
    """Embed docs-typed chunks with the candidate model into a per-key table."""
    from src.index.builders.docs_vector_indexer import build_docs_vectors
    from src.models import EMBEDDING_MODELS

    if key not in EMBEDDING_MODELS:
        print(
            f"  SKIP {key}: not registered in src/models.EMBEDDING_MODELS — extend that registry first",
            file=sys.stderr,
        )
        return {"model_key": key, "skipped_reason": "not_registered"}

    cfg = EMBEDDING_MODELS[key]
    lance_dir = ROOT / "db" / cfg.lance_dir
    checkpoint_path = ROOT / "db" / f"docs_checkpoint_{key}.json"

    avail = _avail_gb()
    if avail < PREFLIGHT_AVAIL_HARD_GB:
        print(
            f"  SKIP {key}: sys-avail={avail:.2f}G fell below {PREFLIGHT_AVAIL_HARD_GB}G mid-run",
            file=sys.stderr,
        )
        return {"model_key": key, "skipped_reason": "low_memory", "avail_gb": avail}

    print()
    print("=" * 60)
    print(f"BUILD CANDIDATE: {key} ({cfg.name}, {cfg.dim}d)")
    print(f"  lance_dir        = {lance_dir}")
    print(f"  checkpoint       = {checkpoint_path}")
    print(f"  query_prefix     = {cfg.query_prefix!r}")
    print(f"  document_prefix  = {cfg.document_prefix!r}")
    print(f"  trust_remote     = {cfg.trust_remote_code}")
    print("=" * 60)

    t0 = time.time()
    try:
        result = build_docs_vectors(
            db_path=DB_PATH,
            lance_dir=lance_dir,
            force=True,
            checkpoint_path=checkpoint_path,
            log_every=2000,
            no_reindex=False,
            pause_daemon=True,
            model_key=key,
        )
    except SystemExit:
        # _memguard fired sys.exit(0); resume manual.
        print(
            f"  {key}: memguard hard-exit; retry sequentially after page release",
            file=sys.stderr,
        )
        return {
            "model_key": key,
            "skipped_reason": "memguard_exit",
            "build_seconds": round(time.time() - t0, 1),
        }
    except Exception as exc:
        print(f"  {key}: build failed: {exc}", file=sys.stderr)
        return {
            "model_key": key,
            "skipped_reason": "exception",
            "exception": str(exc),
            "build_seconds": round(time.time() - t0, 1),
        }

    build_s = time.time() - t0
    size_mb = 0.0
    if lance_dir.exists():
        with contextlib.suppress(Exception):
            size_mb = sum(f.stat().st_size for f in lance_dir.rglob("*") if f.is_file()) / (1024 * 1024)

    return {
        "model_key": key,
        "model_name": cfg.name,
        "dim": cfg.dim,
        "lance_dir": str(lance_dir),
        "lance_size_mb": round(size_mb, 1),
        "chunks_embedded": result.get("chunks_embedded"),
        "vectors_stored": result.get("vectors_stored"),
        "build_seconds": round(build_s, 1),
        **common,
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--only", action="append", help="Build only these keys (repeatable)")
    p.add_argument("--skip", action="append", help="Skip these keys (repeatable)")
    args = p.parse_args()

    skip = set(args.skip or [])
    only = set(args.only or [])
    keys = [k for k in CANDIDATES if (not only or k in only) and k not in skip]
    if not keys:
        print("Nothing to build (all candidates skipped).")
        return 0

    print("=" * 60)
    print(f"Doc-tower A/B build harness — {len(keys)} candidates")
    print(f"Order: {', '.join(keys)}")
    print("=" * 60)
    common = preflight()

    BENCH_DIR.mkdir(parents=True, exist_ok=True)
    ts = dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    summary_path = BENCH_DIR / f"build_summary_{ts}.json"
    summary: list[dict] = []

    for key in keys:
        manifest = build_one(key, common)
        summary.append(manifest)
        per_model_path = BENCH_DIR / f"build_{key}_{ts}.json"
        per_model_path.write_text(json.dumps(manifest, indent=2))
        print(f"  wrote {per_model_path.name}")

        gc.collect()
        try:
            import torch

            if torch.backends.mps.is_available():
                torch.mps.empty_cache()
        except Exception:
            pass

        if key != keys[-1]:
            print(f"  sleeping {BETWEEN_BUILDS_SLEEP_S}s for kernel page release ...")
            time.sleep(BETWEEN_BUILDS_SLEEP_S)

    summary_path.write_text(json.dumps(summary, indent=2))
    print()
    print("=" * 60)
    print(f"DONE. Summary: {summary_path}")
    for m in summary:
        if "skipped_reason" in m:
            print(f"  SKIP {m['model_key']:20s} {m['skipped_reason']}")
        else:
            print(
                f"  OK   {m['model_key']:20s} "
                f"chunks={m.get('chunks_embedded'):>6}  "
                f"vectors={m.get('vectors_stored'):>6}  "
                f"size={m.get('lance_size_mb'):>6.1f}MB  "
                f"t={m.get('build_seconds'):>6.1f}s"
            )
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
