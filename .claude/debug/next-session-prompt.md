# Next session prompt — paste this verbatim

## Context (TL;DR — read first)
- Previous session converged on BASELINE wins (vanilla nomic-v1.5, recall@10 = 0.2509 on de-biased eval-v3)
- 4 candidates rejected, 1 blocked
- Process gains shipped: eval-v3, AND-gate scoreboard, normalize fix, RunPod skeleton
- Budget: $1.70 spent / $13.30 banked
- Full history: ~/.code-rag-mcp/.claude/debug/final-report.md + loop-log.md
- All commits on main: see loop-state.json `commits_landed` field

## Goal of this session
Improve doc-intent recall ABOVE 0.2509 baseline by:
(a) Better fine-tune recipe (hard-negatives + alternatives), OR
(b) Unblocking gte-large-en-v1.5 (intrinsic NTK-rope bug)

## First action: agent debate

Before any code or pod spend, run a 4-team debate. /дебати recipe-improvement.

The debate must produce:
1. Best fine-tune recipe candidate (rank options A1/A2/A3 from p6-pivot-strategist.md, plus new ideas)
2. Concrete plan for gte-large unblock (fork modeling.py, vendor patched copy, OR pivot to other gte-family)
3. Cost-vs-p(win) updated table for each option
4. GO/NO-GO + which option to attempt first

Three teammates:
- recipe-architect (opus): proposes 3-5 fine-tune recipes (hard-negatives, contrastive, distillation, etc.)
- gte-unblocker (opus): diagnoses NTK-rope bug, proposes patches
- skeptic (opus): challenges all proposals; quantifies risk

Lead synthesizes verdict.

## Pre-flight
1. Verify pytest 719/719 green
2. Verify ~/.runpod/credentials still has RUNPOD_API_KEY + HF_TOKEN
3. Verify caffeinate is running (or restart it)
4. Read final-report.md + p6-pivot-strategist.md before debate

## Constraints (non-negotiable)
- $11 effective cap (hold $2 safety margin from $13.30 banked)
- DO NOT modify eval-v3 — it's the new gold; if you need eval changes, write eval-v4 with explicit reason
- DO NOT push to public repo if any artifact contains pay-com data
- Tests stay 719+ green

## Stop conditions for this session
- Two consecutive +10pp Recall@10 lifts confirmed → freeze winner, deploy
- $11 effective cap reached → freeze best-so-far
- 5 iterations no improvement → freeze + write next-next-session prompt

## Reference artifacts
- ~/.code-rag-mcp/.claude/debug/final-report.md — last session summary
- ~/.code-rag-mcp/.claude/debug/loop-log.md — full history
- ~/.code-rag-mcp/.claude/debug/p6-verdict.md — bias root cause
- ~/.code-rag-mcp/profiles/pay-com/doc_intent_eval_v3.jsonl — gold eval
- ~/.code-rag-mcp/scripts/runpod/* — pod tooling
- HF Hub: Tarshevskiy/pay-com-docs-embed-{v0, v1, v1-fixed} — historical
- Memory: project_loop_2026_04_25.md (this loop's full record)

## Open questions for debate
- Is hard-negative mining via FTS5-top-50-minus-gold sufficient, or do we need MS-MARCO-style cross-encoder hard-negatives?
- Should we expand training data beyond 91 pairs? Where do we source more without contaminating eval-v3?
- For gte-large: fork+patch HF modeling.py vs Alibaba-NLP/gte-base-en-v1.5 (smaller cousin, may not have NTK)?
- Is there value in routing improvements (orthogonal axis) BEFORE another fine-tune attempt?
