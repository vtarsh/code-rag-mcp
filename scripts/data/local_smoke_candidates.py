"""Local pre-pod micro-smoke + mini-pipeline for all 6 Run 1 candidates.

For each candidate, three layers run on Mac (CPU/MPS) before any pod spend:

1. **Data parseability**  — train + eval JSONLs open & parse, row counts ≥ floor.
2. **Model load + 1 forward pass** — base loads from HF cache, encode/predict works.
3. **Mini-pipeline** (NEW, 2026-04-26 to catch Bug 6o/6p/6q at $0):
   - Sample 10 train rows → InputExample list.
   - 1 fit() step on CPU (forces MPS-free path; bypasses Mac MPS memory pressure).
   - Save FT'd model to a tempfile.TemporaryDirectory.
   - Reload via SentenceTransformer/CrossEncoder constructor (catches **Bug 6p**:
     state_dict missing keys when reloading FT'd nomic).
   - Encode 5 long chunks (~8000 chars) sampled from `db/knowledge.db`
     (catches **Bug 6q**: CUDA index oob on long-context tokenizer mismatches —
     reproduces on CPU as IndexError / RuntimeError too).
   - Check encode/predict output for NaN/Inf (catches **Bug 6o**: bad vectors
     from FT'd model on edge-case inputs).

NO training to convergence, NO HF push — that runs on the pod. This catches
HF auth issues, arch / trust_remote_code mismatches, missing or corrupted data
files, AND the three live bugs from the 2026-04-26 R1 cycle, before we burn
$ on a pod.

Output is a status table the caller (Claude) uses to decide: all-pass → spawn
6 in parallel, mixed → stream the passing ones to pods one-by-one.

Usage:
  python3.12 scripts/local_smoke_candidates.py
  python3.12 scripts/local_smoke_candidates.py --only=docs-nomic
  python3.12 scripts/local_smoke_candidates.py --skip-train      # skip mini-fit step
  python3.12 scripts/local_smoke_candidates.py --skip-encode     # skip long-chunk encode
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]

# Mini-pipeline knobs. Tuned to fit 6 GB free disk + Mac CPU without thrashing.
MINI_TRAIN_ROWS: int = 10  # 10 rows x 1 step = ~30 s on CPU per docs cand
MINI_FIT_BATCH_SIZE: int = 4  # 10 rows / bs=4 → ~3 batches; 1 step touches one
MINI_FIT_STEPS: int = 1  # one optimiser step is enough to catch state_dict drift
LONG_CHUNK_COUNT: int = 5  # encode 5 long chunks to exercise long-context path
LONG_CHUNK_MIN_LEN: int = 4000  # SQL filter: at least 4 KB content (≈ 1 K tokens)
LONG_CHUNK_TARGET_LEN: int = 8000  # truncate to 8 KB so we don't OOM the tokenizer

# nomic-embed-text-v1.5 requires these prefix tokens at both train + serve time.
# Mirror of NOMIC_QUERY_PREFIX / NOMIC_DOCUMENT_PREFIX in
# scripts/runpod/train_docs_embedder.py — the mini-pipeline must apply them so
# the FT'd weights & reload step exercise the same forward path as the pod run.
NOMIC_QUERY_PREFIX: str = "search_query: "
NOMIC_DOCUMENT_PREFIX: str = "search_document: "


@dataclass(frozen=True)
class Candidate:
    tag: str
    kind: str  # docs | reranker
    base_model: str
    train_data: Path
    eval_data: Path
    hf_repo: str
    needs_trust_remote_code: bool = False
    # MNRL is the canonical R1 docs loss; mxbai/gte/nomic all train with MNRL
    # in the actual pod runs — keep parity here so the mini-pipeline exercises
    # the same loss head that pod training will.
    docs_loss: str = "mnrl"
    # nomic-embed-text-v1.5 needs the search_query: / search_document: prefix
    # at both train and serve time. mxbai + gte do NOT use them.
    apply_nomic_prefix: bool = False


CANDIDATES: tuple[Candidate, ...] = (
    # --- docs tower ----------------------------------------------------------
    Candidate(
        tag="docs-nomic",
        kind="docs",
        base_model="nomic-ai/nomic-embed-text-v1.5",
        train_data=Path("/tmp/r1_cosent_triplets_v3.jsonl"),
        eval_data=REPO_ROOT / "profiles/pay-com/doc_intent_eval_v3.jsonl",
        hf_repo="Tarshevskiy/pay-com-docs-nomic-ft-run1",
        needs_trust_remote_code=True,
        apply_nomic_prefix=True,
    ),
    Candidate(
        tag="docs-mxbai",
        kind="docs",
        base_model="mixedbread-ai/mxbai-embed-large-v1",
        train_data=Path("/tmp/r1_cosent_triplets_v3.jsonl"),
        eval_data=REPO_ROOT / "profiles/pay-com/doc_intent_eval_v3.jsonl",
        hf_repo="Tarshevskiy/pay-com-docs-mxbai-ft-run1",
    ),
    Candidate(
        tag="docs-gte",
        kind="docs",
        base_model="Alibaba-NLP/gte-base-en-v1.5",
        train_data=Path("/tmp/r1_cosent_triplets_v3.jsonl"),
        eval_data=REPO_ROOT / "profiles/pay-com/doc_intent_eval_v3.jsonl",
        hf_repo="Tarshevskiy/pay-com-docs-gte-base-ft-run1",
        needs_trust_remote_code=True,
    ),
    # --- reranker tower -----------------------------------------------------
    Candidate(
        tag="rerank-l12",
        kind="reranker",
        base_model="cross-encoder/ms-marco-MiniLM-L-12-v2",
        train_data=REPO_ROOT / "profiles/pay-com/finetune_data_combined_v1/train.jsonl",
        eval_data=REPO_ROOT / "profiles/pay-com/rerank_pointwise_eval_v1.jsonl",
        hf_repo="Tarshevskiy/pay-com-rerank-l12-ft-run1",
    ),
    Candidate(
        tag="rerank-mxbai",
        kind="reranker",
        base_model="mixedbread-ai/mxbai-rerank-base-v1",
        train_data=REPO_ROOT / "profiles/pay-com/finetune_data_combined_v1/train.jsonl",
        eval_data=REPO_ROOT / "profiles/pay-com/rerank_pointwise_eval_v1.jsonl",
        hf_repo="Tarshevskiy/pay-com-rerank-mxbai-ft-run1",
    ),
    Candidate(
        tag="rerank-bge",
        kind="reranker",
        base_model="BAAI/bge-reranker-v2-m3",
        train_data=REPO_ROOT / "profiles/pay-com/finetune_data_combined_v1/train.jsonl",
        eval_data=REPO_ROOT / "profiles/pay-com/rerank_pointwise_eval_v1.jsonl",
        hf_repo="Tarshevskiy/pay-com-rerank-bge-ft-run1",
    ),
)


def _check_data_parseable(path: Path, min_rows: int) -> tuple[bool, str]:
    if not path.exists():
        return False, f"missing: {path}"
    n = 0
    bad = 0
    with path.open() as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                json.loads(ln)
                n += 1
            except Exception:
                bad += 1
    if n < min_rows:
        return False, f"only {n} rows (want >= {min_rows}); bad={bad}"
    if bad:
        return False, f"{bad} unparseable rows"
    return True, f"{n} rows OK"


def _smoke_docs(cand: Candidate) -> tuple[bool, str]:
    from sentence_transformers import SentenceTransformer

    kwargs: dict[str, Any] = {}
    if cand.needs_trust_remote_code:
        kwargs["trust_remote_code"] = True
    t0 = time.time()
    model = SentenceTransformer(cand.base_model, **kwargs)
    load_s = time.time() - t0
    vec = model.encode("test query for smoke", convert_to_numpy=True)
    if not hasattr(vec, "shape"):
        return False, f"encode returned non-array: {type(vec).__name__}"
    if vec.shape[0] < 64:
        return False, f"embedding dim too small: {vec.shape}"
    return True, f"loaded in {load_s:.1f}s, dim={vec.shape[0]}"


def _smoke_reranker(cand: Candidate) -> tuple[bool, str]:
    from sentence_transformers import CrossEncoder

    kwargs: dict[str, Any] = {}
    if cand.needs_trust_remote_code:
        kwargs["trust_remote_code"] = True
    t0 = time.time()
    model = CrossEncoder(cand.base_model, **kwargs)
    load_s = time.time() - t0
    scores = model.predict([("query test", "doc text")])
    if scores is None or len(scores) != 1:
        return False, f"predict returned wrong shape: {scores!r}"
    score_val = scores[0]
    if not isinstance(score_val, int | float) and not hasattr(score_val, "item"):
        return False, f"predict returned non-numeric: {type(score_val).__name__}"
    return True, f"loaded in {load_s:.1f}s, score={float(score_val):.3f}"


# --------------------------------------------------------------------------- #
# Mini-pipeline (Bug 6o/6p/6q catchers)                                       #
# --------------------------------------------------------------------------- #


def _sample_train_rows_docs(path: Path, n: int, apply_nomic_prefix: bool) -> list[dict]:
    """Read first `n` rows of a docs train JSONL and apply nomic prefix if needed.

    The R1 cosent triplets have shape {query, positive, negative}. For MNRL we
    only feed (query, positive) pairs; for cosent we'd feed both pos+neg. The
    mini-pipeline always uses MNRL because it's the canonical R1 docs loss.
    """
    rows: list[dict] = []
    with path.open() as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            r = json.loads(ln)
            if "query" not in r or "positive" not in r:
                continue
            if apply_nomic_prefix:
                r["query"] = f"{NOMIC_QUERY_PREFIX}{r['query']}"
                r["positive"] = f"{NOMIC_DOCUMENT_PREFIX}{r['positive']}"
                if "negative" in r:
                    r["negative"] = f"{NOMIC_DOCUMENT_PREFIX}{r['negative']}"
            rows.append(r)
            if len(rows) >= n:
                break
    return rows


def _sample_train_rows_reranker(path: Path, n: int) -> list[dict]:
    """Read first `n` rows of a combined-train JSONL ({query, doc, label})."""
    rows: list[dict] = []
    with path.open() as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            r = json.loads(ln)
            if "query" not in r or "doc" not in r or "label" not in r:
                continue
            rows.append(
                {
                    "query": r["query"],
                    "doc": r["doc"],
                    "label": float(r["label"]),
                }
            )
            if len(rows) >= n:
                break
    return rows


def _sample_long_chunks(n: int = LONG_CHUNK_COUNT) -> list[str]:
    """Pull `n` long chunks from db/knowledge.db.

    Truncated to LONG_CHUNK_TARGET_LEN chars so we don't blow up the tokenizer
    on a single edge-case row — the goal is to exercise the long-context
    encode path, not OOM the test. Returns [] if knowledge.db is missing
    (mini-pipeline will skip the long-encode step gracefully).
    """
    db_path = REPO_ROOT / "db" / "knowledge.db"
    if not db_path.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        cur = conn.cursor()
        cur.execute(
            "SELECT content FROM chunks WHERE LENGTH(content) > ? LIMIT ?",
            (LONG_CHUNK_MIN_LEN, n),
        )
        chunks = [row[0][:LONG_CHUNK_TARGET_LEN] for row in cur.fetchall()]
        conn.close()
        return chunks
    except Exception as e:
        print(f"  [mini] WARN long-chunk sample failed: {e}", flush=True)
        return []


def _has_nan_or_inf(arr: Any) -> bool:
    """Check a 1-D or 2-D numpy array (or list) for NaN / Inf values."""
    try:
        import numpy as np

        a = np.asarray(arr)
        if a.dtype.kind == "f":
            return bool(np.isnan(a).any() or np.isinf(a).any())
        return False
    except Exception:
        # Fallback: walk the iterable
        try:
            for x in arr:  # type: ignore[union-attr]
                if hasattr(x, "__iter__"):
                    for y in x:  # type: ignore[union-attr]
                        if math.isnan(float(y)) or math.isinf(float(y)):
                            return True
                else:
                    if math.isnan(float(x)) or math.isinf(float(x)):
                        return True
        except Exception:
            return False
        return False


def _mini_pipeline_docs(cand: Candidate, *, skip_train: bool, skip_encode: bool) -> tuple[bool, str]:
    """Mini training + reload + long-encode pipeline for a docs candidate.

    Sub-steps:
      a. Sample 10 train rows (apply nomic prefix iff applicable).
      b. Build InputExample list and run 1 fit() step on CPU with MNRL.
      c. Save FT'd model to a TemporaryDirectory.
      d. Reload via SentenceTransformer(tmp_dir, trust_remote_code=...). Bug 6p.
      e. Encode 5 long chunks (~8000 chars each). Bug 6q.
      f. Check encode output for NaN/Inf. Bug 6o.

    Returns (ok, message). On any sub-step failure, returns False with a short
    diagnostic so the summary table can show which sub-step failed.
    """
    # Lazy heavy imports — mini-pipeline only runs when smoke step 2 already
    # loaded the base model into HF cache.
    import torch
    from sentence_transformers import InputExample, SentenceTransformer, losses
    from torch.utils.data import DataLoader

    # ---- a. sample rows ----
    rows = _sample_train_rows_docs(cand.train_data, MINI_TRAIN_ROWS, cand.apply_nomic_prefix)
    if len(rows) < MINI_TRAIN_ROWS:
        return False, (
            f"sample: only {len(rows)} rows (want {MINI_TRAIN_ROWS}); "
            f"check train_data shape (need 'query' + 'positive')"
        )

    # ---- load base on CPU ----
    kwargs: dict[str, Any] = {"device": "cpu"}
    if cand.needs_trust_remote_code:
        kwargs["trust_remote_code"] = True
    try:
        model = SentenceTransformer(cand.base_model, **kwargs)
    except Exception as e:
        return False, f"load base on cpu: {type(e).__name__}: {e}"

    # ---- b. 1 fit step (MNRL) ----
    if not skip_train:
        try:
            examples = [InputExample(texts=[r["query"], r["positive"]]) for r in rows]
            loader = DataLoader(  # type: ignore[arg-type]
                examples, shuffle=True, batch_size=MINI_FIT_BATCH_SIZE
            )
            loss_fn = losses.MultipleNegativesRankingLoss(model)
            fit_kwargs: dict[str, Any] = {
                "train_objectives": [(loader, loss_fn)],
                "epochs": 1,
                "warmup_steps": 0,
                "optimizer_params": {"lr": 2e-5},
                "show_progress_bar": False,
            }
            try:
                model.fit(**fit_kwargs, steps_per_epoch=MINI_FIT_STEPS)
            except TypeError:
                model.fit(**fit_kwargs)
        except Exception as e:
            tb = traceback.format_exc().splitlines()[-2:]
            return False, f"mini-fit: {type(e).__name__}: {e} | {'; '.join(tb)}"

    # ---- c + d. save and reload ----
    reload_kwargs: dict[str, Any] = {"device": "cpu"}
    if cand.needs_trust_remote_code:
        reload_kwargs["trust_remote_code"] = True
    with tempfile.TemporaryDirectory(prefix=f"smoke_{cand.tag}_") as tmpd:
        try:
            model.save(tmpd)
        except Exception as e:
            return False, f"save: {type(e).__name__}: {e}"

        # Free the trained model before reload to dodge double-RAM peak.
        del model
        import contextlib

        with contextlib.suppress(Exception):
            torch.mps.empty_cache()  # no-op on CPU; safe call

        try:
            reloaded = SentenceTransformer(tmpd, **reload_kwargs)
        except Exception as e:
            tb = traceback.format_exc().splitlines()[-3:]
            return False, (f"reload (Bug 6p): {type(e).__name__}: {e} | {'; '.join(tb)}")

        # ---- e. encode long chunks ----
        if not skip_encode:
            chunks = _sample_long_chunks()
            if not chunks:
                # If knowledge.db missing, fall back to a short-text encode just
                # to prove reload yields a working model.
                fallback = ["short test query"]
                try:
                    vec = reloaded.encode(fallback, convert_to_numpy=True)
                except Exception as e:
                    return False, f"encode-fallback: {type(e).__name__}: {e}"
                if _has_nan_or_inf(vec):
                    return False, "encode-fallback (Bug 6o): NaN/Inf in vector"
                return True, "OK (no long chunks; short-fallback encoded)"
            try:
                vecs = reloaded.encode(chunks, convert_to_numpy=True)
            except Exception as e:
                tb = traceback.format_exc().splitlines()[-3:]
                return False, (f"long-encode (Bug 6q): {type(e).__name__}: {e} | {'; '.join(tb)}")
            # ---- f. NaN/Inf check ----
            if _has_nan_or_inf(vecs):
                return False, ("long-encode (Bug 6o): NaN/Inf in vectors after reload")
            return True, (f"OK (fit+save+reload+long-encode {len(chunks)} chunks, dim={vecs.shape[-1]})")

        return True, "OK (fit+save+reload, encode skipped)"


def _mini_pipeline_reranker(cand: Candidate, *, skip_train: bool, skip_encode: bool) -> tuple[bool, str]:
    """Mini training + reload + long-pair predict pipeline for a reranker.

    Sub-steps:
      a. Sample 10 combined-train rows ({query, doc, label}).
      b. CrossEncoder.fit() for 1 step on CPU.
      c. Save → reload via CrossEncoder(tmp_dir).
      d. Predict 5 long pairs.
      e. Verify scores are floats (not NaN / Inf).
    """
    import torch
    from sentence_transformers import CrossEncoder, InputExample
    from torch.utils.data import DataLoader

    # ---- a. sample rows ----
    rows = _sample_train_rows_reranker(cand.train_data, MINI_TRAIN_ROWS)
    if len(rows) < MINI_TRAIN_ROWS:
        return False, (
            f"sample: only {len(rows)} rows (want {MINI_TRAIN_ROWS}); "
            f"check train_data shape (need 'query'+'doc'+'label')"
        )

    # ---- load base on CPU (num_labels=1 to mirror pod CE recipe) ----
    kwargs: dict[str, Any] = {"num_labels": 1, "device": "cpu"}
    if cand.needs_trust_remote_code:
        kwargs["trust_remote_code"] = True
    try:
        model = CrossEncoder(cand.base_model, **kwargs)
    except Exception as e:
        return False, f"load base on cpu: {type(e).__name__}: {e}"

    # ---- b. 1 fit step ----
    if not skip_train:
        try:
            examples = [InputExample(texts=[r["query"], r["doc"]], label=float(r["label"])) for r in rows]
            loader = DataLoader(  # type: ignore[arg-type]
                examples, shuffle=True, batch_size=MINI_FIT_BATCH_SIZE
            )
            # CrossEncoder.fit signature varies by ST version. Drop kwargs the
            # current install rejects so the smoke runs across versions.
            fit_kwargs: dict[str, Any] = {
                "train_dataloader": loader,
                "epochs": 1,
                "warmup_steps": 0,
                "optimizer_params": {"lr": 2e-5},
                "show_progress_bar": False,
            }
            try:
                model.fit(**fit_kwargs, steps_per_epoch=MINI_FIT_STEPS)
            except TypeError:
                # Older / newer ST without steps_per_epoch — fit on the
                # tiny loader (it has only MINI_FIT_BATCH_SIZE rows so the
                # epoch is one batch).
                model.fit(**fit_kwargs)
        except Exception as e:
            tb = traceback.format_exc().splitlines()[-2:]
            return False, f"mini-fit: {type(e).__name__}: {e} | {'; '.join(tb)}"

    # ---- c + d. save + reload ----
    with tempfile.TemporaryDirectory(prefix=f"smoke_{cand.tag}_") as tmpd:
        try:
            model.save(tmpd)
        except Exception as e:
            return False, f"save: {type(e).__name__}: {e}"

        del model
        import contextlib

        with contextlib.suppress(Exception):
            torch.mps.empty_cache()

        reload_kwargs: dict[str, Any] = {"device": "cpu"}
        if cand.needs_trust_remote_code:
            reload_kwargs["trust_remote_code"] = True
        try:
            reloaded = CrossEncoder(tmpd, **reload_kwargs)
        except Exception as e:
            tb = traceback.format_exc().splitlines()[-3:]
            return False, (f"reload (Bug 6p): {type(e).__name__}: {e} | {'; '.join(tb)}")

        # ---- e. predict on long pairs ----
        if not skip_encode:
            chunks = _sample_long_chunks()
            if chunks:
                pairs = [(rows[i % len(rows)]["query"], chunks[i]) for i in range(len(chunks))]
            else:
                # Fallback to in-row pairs if no knowledge.db
                pairs = [(r["query"], r["doc"]) for r in rows[:5]]
            try:
                scores = reloaded.predict(pairs)
            except Exception as e:
                tb = traceback.format_exc().splitlines()[-3:]
                return False, (f"long-predict (Bug 6q): {type(e).__name__}: {e} | {'; '.join(tb)}")
            if _has_nan_or_inf(scores):
                return False, "long-predict (Bug 6o): NaN/Inf in scores"
            return True, (f"OK (fit+save+reload+predict {len(pairs)} pairs)")

        return True, "OK (fit+save+reload, predict skipped)"


def _run_mini_pipeline(cand: Candidate, *, skip_train: bool, skip_encode: bool) -> dict[str, Any]:
    """Wrapper: dispatch to docs/reranker mini-pipeline and capture exceptions.

    Returns a dict with `ok` (bool) and `msg` (str). Never raises — a crash in
    the pipeline becomes ok=False so the per-candidate run continues to the
    summary table without aborting the whole sweep.
    """
    t0 = time.time()
    try:
        if cand.kind == "docs":
            ok, msg = _mini_pipeline_docs(cand, skip_train=skip_train, skip_encode=skip_encode)
        else:
            ok, msg = _mini_pipeline_reranker(cand, skip_train=skip_train, skip_encode=skip_encode)
        elapsed = time.time() - t0
        return {"ok": ok, "msg": msg, "elapsed_s": round(elapsed, 1)}
    except Exception as e:
        elapsed = time.time() - t0
        tb = traceback.format_exc().splitlines()[-3:]
        return {
            "ok": False,
            "msg": f"{type(e).__name__}: {e}",
            "tb": tb,
            "elapsed_s": round(elapsed, 1),
        }


def smoke(cand: Candidate, *, skip_train: bool = False, skip_encode: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {"tag": cand.tag, "kind": cand.kind, "base_model": cand.base_model}

    # Step 1: data parseability
    min_train = 100 if cand.kind == "docs" else 1000
    ok_train, msg_train = _check_data_parseable(cand.train_data, min_train)
    out["train_data"] = {"ok": ok_train, "msg": msg_train, "path": str(cand.train_data)}
    ok_eval, msg_eval = _check_data_parseable(cand.eval_data, 10)
    out["eval_data"] = {"ok": ok_eval, "msg": msg_eval, "path": str(cand.eval_data)}

    if not (ok_train and ok_eval):
        out["model_load"] = {"ok": False, "msg": "skipped: data check failed"}
        out["mini_pipeline"] = {"ok": False, "msg": "skipped: data check failed"}
        out["overall_ok"] = False
        return out

    # Step 2: model load + forward pass
    try:
        if cand.kind == "docs":
            ok_model, msg_model = _smoke_docs(cand)
        else:
            ok_model, msg_model = _smoke_reranker(cand)
        out["model_load"] = {"ok": ok_model, "msg": msg_model}
    except Exception as e:
        tb = traceback.format_exc().splitlines()[-3:]
        out["model_load"] = {"ok": False, "msg": f"{type(e).__name__}: {e}", "tb": tb}
        out["mini_pipeline"] = {"ok": False, "msg": "skipped: model load failed"}
        out["overall_ok"] = False
        return out

    if not ok_model:
        out["mini_pipeline"] = {"ok": False, "msg": "skipped: model load failed"}
        out["overall_ok"] = False
        return out

    # Step 3: mini-pipeline (fit + save + reload + long-encode)
    mini = _run_mini_pipeline(cand, skip_train=skip_train, skip_encode=skip_encode)
    out["mini_pipeline"] = mini

    out["overall_ok"] = ok_train and ok_eval and ok_model and bool(mini.get("ok"))
    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--only", help="Run only this tag (e.g. docs-nomic)")
    p.add_argument(
        "--skip-train",
        action="store_true",
        help="Skip the 1-step mini-fit (still does load + save + reload + encode)",
    )
    p.add_argument(
        "--skip-encode",
        action="store_true",
        help="Skip the long-chunk encode step (still runs fit + save + reload)",
    )
    args = p.parse_args()

    targets = CANDIDATES
    if args.only:
        targets = tuple(c for c in CANDIDATES if c.tag == args.only)
        if not targets:
            sys.exit(f"--only={args.only!r} matched no candidate")

    print(f"=== Local smoke for {len(targets)} candidate(s) ===", flush=True)
    print(
        "Note: first-time HF model download may take 1-15 min per model "
        "(bge-reranker-v2-m3 ~2.2 GB, mxbai-embed-large-v1 ~1.3 GB).\n"
        "Mini-pipeline runs on CPU to dodge MPS memory pressure (~30-90 s per cand).\n",
        flush=True,
    )

    results: list[dict[str, Any]] = []
    for cand in targets:
        print(f"\n--- {cand.tag} ({cand.kind}: {cand.base_model}) ---", flush=True)
        t0 = time.time()
        res = smoke(cand, skip_train=args.skip_train, skip_encode=args.skip_encode)
        elapsed = time.time() - t0
        res["elapsed_s"] = round(elapsed, 1)

        # Streaming print
        for stage in ("train_data", "eval_data", "model_load", "mini_pipeline"):
            section = res.get(stage, {})
            mark = "OK" if section.get("ok") else "FAIL"
            print(f"  [{mark}] {stage}: {section.get('msg', '')}", flush=True)
        overall = "PASS" if res["overall_ok"] else "FAIL"
        print(f"  -> {overall}  ({elapsed:.1f}s)", flush=True)
        results.append(res)

    # Final table
    print("\n=== Summary ===", flush=True)
    n_pass = sum(1 for r in results if r["overall_ok"])
    print(f"PASS={n_pass}  FAIL={len(results) - n_pass}", flush=True)
    print(
        "\nTag             Kind      Load     Mini-pipe Result   Elapsed",
        flush=True,
    )
    for r in results:
        mark = "PASS" if r["overall_ok"] else "FAIL"
        load_mark = "OK" if r.get("model_load", {}).get("ok") else "FAIL"
        mini_mark = "OK" if r.get("mini_pipeline", {}).get("ok") else "FAIL"
        print(
            f"  {r['tag']:<15} {r['kind']:<9} {load_mark:<8} {mini_mark:<9} {mark:<8} {r['elapsed_s']}s",
            flush=True,
        )

    out_path = REPO_ROOT / ".claude" / "debug" / "local_smoke_results.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(results, indent=2))
    print(f"\nDetail written to {out_path}", flush=True)
    return 0 if n_pass == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
