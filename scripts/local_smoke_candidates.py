"""Local pre-pod micro-smoke for all 6 Run 1 candidates.

For each candidate: load HF base model on Mac (CPU/MPS), do one forward
pass (encode for docs tower, predict for reranker), then verify
train_data + eval_data are parseable. NO training, NO HF push - that
runs on the pod. This catches HF auth issues, arch / trust_remote_code
mismatches, missing or corrupted data files, before we burn $ on a pod.

Per user requirement: "локальний мікропрогін, просто тест, що все
запускається на кожному з етапів". Output is a status table the
caller (Claude) uses to decide: all-pass → spawn 6 in parallel,
mixed → stream the passing ones to pods one-by-one.

Usage:
  python3.12 scripts/local_smoke_candidates.py
  python3.12 scripts/local_smoke_candidates.py --only=docs-nomic
  python3.12 scripts/local_smoke_candidates.py --skip-download-hint
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Candidate:
    tag: str
    kind: str  # docs | reranker
    base_model: str
    train_data: Path
    eval_data: Path
    hf_repo: str
    needs_trust_remote_code: bool = False


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


def smoke(cand: Candidate) -> dict[str, Any]:
    out: dict[str, Any] = {"tag": cand.tag, "kind": cand.kind, "base_model": cand.base_model}

    # Step 1: data parseability
    min_train = 100 if cand.kind == "docs" else 1000
    ok_train, msg_train = _check_data_parseable(cand.train_data, min_train)
    out["train_data"] = {"ok": ok_train, "msg": msg_train, "path": str(cand.train_data)}
    ok_eval, msg_eval = _check_data_parseable(cand.eval_data, 10)
    out["eval_data"] = {"ok": ok_eval, "msg": msg_eval, "path": str(cand.eval_data)}

    if not (ok_train and ok_eval):
        out["model_load"] = {"ok": False, "msg": "skipped: data check failed"}
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
        out["overall_ok"] = False
        return out

    out["overall_ok"] = ok_train and ok_eval and ok_model
    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--only", help="Run only this tag (e.g. docs-nomic)")
    args = p.parse_args()

    targets = CANDIDATES
    if args.only:
        targets = tuple(c for c in CANDIDATES if c.tag == args.only)
        if not targets:
            sys.exit(f"--only={args.only!r} matched no candidate")

    print(f"=== Local smoke for {len(targets)} candidate(s) ===", flush=True)
    print(
        "Note: first-time HF model download may take 1-15 min per model "
        "(bge-reranker-v2-m3 ~2.2 GB, mxbai-embed-large-v1 ~1.3 GB).\n",
        flush=True,
    )

    results: list[dict[str, Any]] = []
    for cand in targets:
        print(f"\n--- {cand.tag} ({cand.kind}: {cand.base_model}) ---", flush=True)
        t0 = time.time()
        res = smoke(cand)
        elapsed = time.time() - t0
        res["elapsed_s"] = round(elapsed, 1)

        # Streaming print
        for stage in ("train_data", "eval_data", "model_load"):
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
    print("\nTag             Kind      Result   Elapsed", flush=True)
    for r in results:
        mark = "PASS" if r["overall_ok"] else "FAIL"
        print(f"  {r['tag']:<15} {r['kind']:<9} {mark:<8} {r['elapsed_s']}s", flush=True)

    out_path = REPO_ROOT / ".claude" / "debug" / "local_smoke_results.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(results, indent=2))
    print(f"\nDetail written to {out_path}", flush=True)
    return 0 if n_pass == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
