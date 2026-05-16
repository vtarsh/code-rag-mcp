#!/usr/bin/env python3
"""Autoresearch loop for scalar hyperparameter tuning.

Quick-start (grid search repo_prefilter_boost):
    python3 scripts/autoresearch.py --knob repo_prefilter_boost --grid 1.0,1.2,1.4,1.6,1.8

Random search (perturb one knob per iteration):
    python3 scripts/autoresearch.py --knob keyword_weight --random --iters 20

Safety:
  - Snapshot/restore conventions.yaml automatically.
  - Writes all results to logs/autoresearch.jsonl.
  - Kill switch: Ctrl-C restores conventions.yaml.
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

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts._common import setup_paths

setup_paths()

BASE = Path(os.getenv("CODE_RAG_HOME", Path.home() / ".code-rag-mcp"))
PROFILE = os.getenv("ACTIVE_PROFILE", "pay-com")
PROFILE_DIR = BASE / "profiles" / PROFILE
CONVENTIONS = PROFILE_DIR / "conventions.yaml"
LOG_DIR = BASE / "logs"
LOG_FILE = LOG_DIR / "autoresearch.jsonl"
SNAPSHOT = Path("/tmp") / f"conventions.yaml.autoresearch.{os.getpid()}.bak"

# Knobs that support env-var override (no conventions.yaml mutation needed).
ENV_KNOBS = {
    "repo_prefilter_boost": (float, 1.0, 3.0, "CODE_RAG_REPO_PREFILTER_BOOST"),
    "repo_prefilter_top_k": (int, 1, 10, "CODE_RAG_REPO_PREFILTER_TOP_K"),
    "rerank_pool_size": (int, 20, 500, "CODE_RAG_RERANK_POOL_SIZE"),
    "doc_penalty": (float, 0.0, 1.0, "CODE_RAG_DOC_PENALTY"),
    "test_penalty": (float, 0.0, 1.0, "CODE_RAG_TEST_PENALTY"),
    "guide_penalty": (float, 0.0, 1.0, "CODE_RAG_GUIDE_PENALTY"),
    "ci_penalty": (float, 0.0, 1.0, "CODE_RAG_CI_PENALTY"),
}

# Knobs that require conventions.yaml mutation (no env override).
FILE_KNOBS = {
    "rrf_k": (int, 10, 200),
    "keyword_weight": (float, 0.5, 5.0),
    "gotchas_boost": (float, 0.5, 3.0),
    "reference_boost": (float, 0.5, 3.0),
    "dictionary_boost": (float, 0.5, 3.0),
    "code_fact_boost": (float, 0.5, 3.0),
    "code_fact_inject_weight": (float, 0.1, 1.0),
    "env_var_boost": (float, 0.5, 3.0),
}

ALL_KNOBS = {**ENV_KNOBS, **FILE_KNOBS}

EVAL_SCRIPT = REPO_ROOT / "scripts" / "eval" / "eval_jira_clean.py"


@dataclass
class Trial:
    knob: str
    value: float | int
    hit_at_10: float | None = None
    recall_at_10: float | None = None
    ndcg_at_10: float | None = None
    elapsed_sec: float = 0.0
    out_path: str = ""
    error: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Config mutation
# ---------------------------------------------------------------------------


def read_tuning_value(knob: str) -> float | int:
    """Read current value from conventions.yaml (FILE_KNOBS only)."""
    text = CONVENTIONS.read_text()
    m = re.search(rf"^\s+{knob}:\s*([\d.]+)", text, re.M)
    if not m:
        raise RuntimeError(f"Knob '{knob}' not found in {CONVENTIONS}")
    kind = FILE_KNOBS[knob][0]
    return int(m.group(1)) if kind is int else float(m.group(1))


def write_tuning_value(knob: str, value: float | int) -> None:
    """Write new value to conventions.yaml (FILE_KNOBS only)."""
    text = CONVENTIONS.read_text()
    pattern = rf"^(\s+{knob}:\s*)[\d.]+"
    kind = FILE_KNOBS[knob][0]
    fmt = str(int(value)) if kind is int else f"{value:.3f}"
    text, n = re.subn(pattern, rf"\g<1>{fmt}", text, count=1, flags=re.M)
    if n != 1:
        raise RuntimeError(f"Failed to replace knob '{knob}'")
    CONVENTIONS.write_text(text)


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def run_eval(knob: str, value: float | int, env_vars: dict[str, str] | None = None) -> Trial:
    """Run one eval trial. Returns Trial with metrics."""
    start = time.time()
    out_path = LOG_DIR / f"autoresearch_{knob}_{value}_{int(start)}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    if env_vars:
        env.update(env_vars)

    cmd = [
        sys.executable,
        str(EVAL_SCRIPT),
        "--out",
        str(out_path),
        "--limit",
        "10",
    ]

    trial = Trial(knob=knob, value=value)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=1800)
        if result.returncode != 0:
            trial.error = result.stderr[:500]
            return trial
        data = json.loads(out_path.read_text())
        agg = data.get("aggregates", {})
        trial.hit_at_10 = agg.get("hit_at_10")
        trial.recall_at_10 = agg.get("recall_at_10")
        trial.ndcg_at_10 = agg.get("ndcg_at_10")
        trial.out_path = str(out_path)
    except subprocess.TimeoutExpired:
        trial.error = "timeout"
    except Exception as exc:
        trial.error = str(exc)[:500]

    trial.elapsed_sec = round(time.time() - start, 1)
    return trial


def log_trial(trial: Trial) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a") as f:
        f.write(json.dumps(trial.as_dict()) + "\n")


# ---------------------------------------------------------------------------
# Search strategies
# ---------------------------------------------------------------------------


def grid_search(knob: str, values: list[float | int]) -> list[Trial]:
    """Run eval for each value in the grid."""
    trials: list[Trial] = []
    is_env = knob in ENV_KNOBS

    for val in values:
        print(f"\n[{len(trials) + 1}/{len(values)}] {knob} = {val}")
        if is_env:
            env_var = ENV_KNOBS[knob][3]
            trial = run_eval(knob, val, {env_var: str(val)})
        else:
            write_tuning_value(knob, val)
            trial = run_eval(knob, val)
        log_trial(trial)
        if trial.error:
            print(f"  ERROR: {trial.error}")
        else:
            print(
                f"  hit@10={trial.hit_at_10:.2%}  recall@10={trial.recall_at_10:.2%}  "
                f"nDCG@10={trial.ndcg_at_10:.2%}  ({trial.elapsed_sec}s)"
            )
        trials.append(trial)
    return trials


def random_search(knob: str, iters: int, seed: int) -> list[Trial]:
    """Random single-knob perturbation within bounds."""
    rng = random.Random(seed)
    kind, lo, hi = ALL_KNOBS[knob][:3]
    is_env = knob in ENV_KNOBS

    trials: list[Trial] = []
    for i in range(iters):
        if kind is int:
            val = rng.randint(lo, hi)
        else:
            val = round(rng.uniform(lo, hi), 3)
        print(f"\n[{i + 1}/{iters}] {knob} = {val}")
        if is_env:
            env_var = ENV_KNOBS[knob][3]
            trial = run_eval(knob, val, {env_var: str(val)})
        else:
            write_tuning_value(knob, val)
            trial = run_eval(knob, val)
        log_trial(trial)
        if trial.error:
            print(f"  ERROR: {trial.error}")
        else:
            print(
                f"  hit@10={trial.hit_at_10:.2%}  recall@10={trial.recall_at_10:.2%}  "
                f"nDCG@10={trial.ndcg_at_10:.2%}  ({trial.elapsed_sec}s)"
            )
        trials.append(trial)
    return trials


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--knob", required=True, choices=list(ALL_KNOBS.keys()))
    p.add_argument("--grid", help="Comma-separated values (e.g. 1.0,1.2,1.4)")
    p.add_argument("--random", action="store_true", help="Random search mode")
    p.add_argument("--iters", type=int, default=10, help="Iterations for random search")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--restore", action="store_true", help="Only restore conventions.yaml snapshot and exit")
    args = p.parse_args()

    if args.restore:
        if SNAPSHOT.exists():
            shutil.copy2(SNAPSHOT, CONVENTIONS)
            print(f"Restored {CONVENTIONS} from {SNAPSHOT}")
        else:
            print(f"No snapshot found at {SNAPSHOT}")
        return 0

    if not CONVENTIONS.exists():
        print(f"ERROR: {CONVENTIONS} not found", file=sys.stderr)
        return 1

    if not EVAL_SCRIPT.exists():
        print(f"ERROR: {EVAL_SCRIPT} not found", file=sys.stderr)
        return 1

    # Snapshot conventions.yaml before any mutation
    shutil.copy2(CONVENTIONS, SNAPSHOT)
    print(f"Snapshot: {SNAPSHOT}")

    try:
        if args.grid:
            values = [int(v) if ALL_KNOBS[args.knob][0] is int else float(v) for v in args.grid.split(",")]
            trials = grid_search(args.knob, values)
        elif args.random:
            trials = random_search(args.knob, args.iters, args.seed)
        else:
            print("ERROR: specify --grid or --random", file=sys.stderr)
            return 1
    except KeyboardInterrupt:
        print("\nInterrupted — restoring conventions.yaml ...")
        shutil.copy2(SNAPSHOT, CONVENTIONS)
        print("Restored.")
        return 130
    finally:
        # Always restore conventions.yaml on exit
        if SNAPSHOT.exists():
            shutil.copy2(SNAPSHOT, CONVENTIONS)
            print(f"Restored {CONVENTIONS}")

    # Report best
    ok_trials = [t for t in trials if t.hit_at_10 is not None]
    if ok_trials:
        best = max(ok_trials, key=lambda t: t.hit_at_10 or 0)
        print(f"\n=== BEST === {best.knob} = {best.value}  hit@10={best.hit_at_10:.2%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
