#!/usr/bin/env python3
"""Karpathy-style autoresearch loop for RAG scalar tuning.

Constrained to 4 scalar knobs under `tuning:` in profiles/{PROFILE}/conventions.yaml:
  rrf_k, keyword_weight, gotchas_boost, reference_boost

Loop: propose perturbation → apply → run benchmark → keep if improved else revert.

v0 strategy: random single-knob perturbation (no LLM). Establishes the harness
before we invest in an LLM strategist.

Safety:
  - Snapshots conventions.yaml before run; always restores on exit.
  - Modifies ONLY the 4 knob lines under tuning:. Refuses to touch anything else.
  - Writes to logs/autoresearch.jsonl for post-hoc analysis.

Usage:
  python3 scripts/autoresearch_loop.py --iters 10 --benchmark queries
  python3 scripts/autoresearch_loop.py --iters 20 --benchmark realworld --seed 42
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

BASE_DIR = Path(os.environ.get("CODE_RAG_HOME", Path.home() / ".code-rag-mcp"))
PROFILE = os.environ.get("ACTIVE_PROFILE", "pay-com")
PROFILE_DIR = BASE_DIR / "profiles" / PROFILE
CONVENTIONS = PROFILE_DIR / "conventions.yaml"
LOG_DIR = BASE_DIR / "logs"
LOG_FILE = LOG_DIR / "autoresearch.jsonl"
SNAPSHOT = Path("/tmp") / f"conventions.yaml.autoresearch.{os.getpid()}.bak"

# Bounded ranges — anything outside these is refused.
KNOB_RANGES = {
    "rrf_k": (int, 10, 200),
    "keyword_weight": (float, 0.5, 5.0),
    "gotchas_boost": (float, 0.5, 3.0),
    "reference_boost": (float, 0.5, 3.0),
}

BENCHMARKS = {
    "queries": {
        "cmd": ["python3", "scripts/benchmark_queries.py"],
        "regex": r"Average composite score[.\s]*\s*([\d.]+)",
    },
    "realworld": {"cmd": ["python3", "scripts/benchmark_realworld.py"], "regex": r"Average[^:]*:\s*([\d.]+)"},
    "mrr": {"cmd": ["python3", "scripts/autoresearch_eval.py"], "regex": r"Average MRR score:\s*([\d.]+)"},
}


@dataclass
class Tuning:
    rrf_k: int
    keyword_weight: float
    gotchas_boost: float
    reference_boost: float

    def as_dict(self) -> dict:
        return asdict(self)


def read_tuning() -> Tuning:
    text = CONVENTIONS.read_text()
    vals = {}
    for knob in KNOB_RANGES:
        m = re.search(rf"^\s+{knob}:\s*([\d.]+)", text, re.M)
        if not m:
            raise RuntimeError(f"Knob '{knob}' not found in {CONVENTIONS}")
        vals[knob] = int(m.group(1)) if KNOB_RANGES[knob][0] is int else float(m.group(1))
    return Tuning(**vals)


def write_tuning(t: Tuning) -> None:
    text = CONVENTIONS.read_text()
    for knob, val in t.as_dict().items():
        pattern = rf"^(\s+{knob}:\s*)[\d.]+"
        fmt = str(int(val)) if KNOB_RANGES[knob][0] is int else f"{val:.3f}"
        text, n = re.subn(pattern, rf"\g<1>{fmt}", text, count=1, flags=re.M)
        if n != 1:
            raise RuntimeError(f"Failed to replace knob '{knob}'")
    CONVENTIONS.write_text(text)


def propose(current: Tuning, rng: random.Random) -> tuple[str, Tuning]:
    """Pick one knob and perturb it by ±10-40% within bounds."""
    knob = rng.choice(list(KNOB_RANGES.keys()))
    kind, lo, hi = KNOB_RANGES[knob]
    cur_val = getattr(current, knob)
    delta_pct = rng.uniform(-0.4, 0.4)
    new_val = cur_val * (1 + delta_pct)
    new_val = max(lo, min(hi, new_val))
    if kind is int:
        new_val = max(round(new_val), lo)
    else:
        new_val = round(new_val, 3)
    if new_val == cur_val:
        # Force a small shift so we don't loop on identical values.
        new_val = cur_val + (1 if kind is int else 0.01) * (1 if delta_pct >= 0 else -1)
        new_val = max(lo, min(hi, new_val))
    new = Tuning(**{**current.as_dict(), knob: new_val})
    return knob, new


def run_benchmark(name: str) -> tuple[float | None, float, str]:
    """Execute benchmark, parse score. Returns (score, duration_s, raw_tail)."""
    spec = BENCHMARKS[name]
    env = {
        **os.environ,
        "CODE_RAG_HOME": str(BASE_DIR),
        "ACTIVE_PROFILE": PROFILE,
    }
    t0 = time.time()
    proc = subprocess.run(
        spec["cmd"],
        cwd=BASE_DIR,
        env=env,
        capture_output=True,
        text=True,
    )
    dur = time.time() - t0
    output = proc.stdout + proc.stderr
    tail = "\n".join(output.splitlines()[-40:])
    m = re.search(spec["regex"], output)
    score = float(m.group(1)) if m else None
    return score, dur, tail


def log(entry: dict) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iters", type=int, default=10)
    parser.add_argument("--benchmark", choices=list(BENCHMARKS), default="queries")
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument(
        "--min-delta", type=float, default=0.0, help="Minimum score improvement to accept (default: any improvement)"
    )
    args = parser.parse_args()

    rng = random.Random(args.seed)

    if not CONVENTIONS.exists():
        print(f"FATAL: {CONVENTIONS} not found", file=sys.stderr)
        return 1

    # Snapshot the file (not just tuning section — full content, to be safe on restore).
    shutil.copy2(CONVENTIONS, SNAPSHOT)
    print(f"[autoresearch] snapshot → {SNAPSHOT}")

    run_id = f"run-{int(time.time())}"
    start_tuning = read_tuning()
    print(f"[autoresearch] baseline tuning: {start_tuning.as_dict()}")

    try:
        # Baseline measurement
        print(f"[autoresearch] measuring baseline on benchmark={args.benchmark}...")
        base_score, base_dur, base_tail = run_benchmark(args.benchmark)
        if base_score is None:
            print(f"FATAL: baseline benchmark failed to produce a score. Tail:\n{base_tail}", file=sys.stderr)
            return 2
        print(f"[autoresearch] baseline score: {base_score:.4f}  ({base_dur:.1f}s)")
        log(
            {
                "run": run_id,
                "iter": 0,
                "event": "baseline",
                "score": base_score,
                "duration_s": base_dur,
                "tuning": start_tuning.as_dict(),
                "benchmark": args.benchmark,
            }
        )

        best_score = base_score
        best_tuning = start_tuning
        history = [{"iter": 0, "score": base_score, "tuning": start_tuning.as_dict(), "action": "baseline"}]

        for i in range(1, args.iters + 1):
            knob, proposed = propose(best_tuning, rng)
            write_tuning(proposed)
            score, dur, _tail = run_benchmark(args.benchmark)
            if score is None:
                action = "ERROR_PARSE"
                delta = None
                print(f"[autoresearch] iter={i} {knob}={getattr(proposed, knob)} → PARSE ERROR ({dur:.1f}s)")
            else:
                delta = score - best_score
                if delta > args.min_delta:
                    action = "KEEP"
                    best_score = score
                    best_tuning = proposed
                    print(
                        f"[autoresearch] iter={i} {knob}={getattr(proposed, knob)} → {score:.4f}  Δ={delta:+.4f}  KEEP  ({dur:.1f}s)"
                    )
                else:
                    action = "REJECT"
                    write_tuning(best_tuning)  # revert
                    print(
                        f"[autoresearch] iter={i} {knob}={getattr(proposed, knob)} → {score:.4f}  Δ={delta:+.4f}  REJECT ({dur:.1f}s)"
                    )
            log(
                {
                    "run": run_id,
                    "iter": i,
                    "event": action,
                    "knob": knob,
                    "proposed": proposed.as_dict(),
                    "score": score,
                    "delta": delta,
                    "duration_s": dur,
                    "benchmark": args.benchmark,
                }
            )
            history.append({"iter": i, "score": score, "tuning": proposed.as_dict(), "action": action, "knob": knob})

        # Final: restore original file formatting if no improvement; else apply best.
        improvement = best_score - base_score
        if improvement <= 0:
            shutil.copy2(SNAPSHOT, CONVENTIONS)
        else:
            write_tuning(best_tuning)
        print()
        print("=" * 60)
        print(f"[autoresearch] DONE  baseline={base_score:.4f}  best={best_score:.4f}  Δ={improvement:+.4f}")
        print(f"[autoresearch] best tuning: {best_tuning.as_dict()}")
        print(f"[autoresearch] kept {sum(1 for h in history if h['action'] == 'KEEP')}/{args.iters} iterations")
        print("=" * 60)
        log(
            {
                "run": run_id,
                "iter": args.iters,
                "event": "final",
                "baseline": base_score,
                "best": best_score,
                "delta": improvement,
                "best_tuning": best_tuning.as_dict(),
            }
        )
        return 0
    except KeyboardInterrupt:
        print("\n[autoresearch] interrupted — restoring snapshot", file=sys.stderr)
        shutil.copy2(SNAPSHOT, CONVENTIONS)
        return 130
    except Exception as e:
        print(f"\n[autoresearch] ERROR: {e} — restoring snapshot", file=sys.stderr)
        shutil.copy2(SNAPSHOT, CONVENTIONS)
        raise


if __name__ == "__main__":
    sys.exit(main())
