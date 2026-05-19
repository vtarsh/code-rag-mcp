# Architecture status — code-rag-mcp · 2026-05-19

> **READ THIS FIRST.** This is the current source of truth on direction.
> It SUPERSEDES `MODEL_TRAINING_SPEC.md`, `RERANKER_IMPROVEMENT_PLAN.md`,
> `NEXT_SESSION_PROMPT.md` and the recall@10 framing in `SESSION_FINDINGS.md` —
> all of those reflect an earlier direction that was tested and rejected.

## TL;DR

The recall@10 chase and the reranker / embedding fine-tuning plan were tested
and **rejected**. The system is a working hybrid. The open question is **"keep
the hybrid as-is"** vs **"full agentic-grep rebuild"** — a real re-architecture,
not a free simplification.

## DO NOT (new sessions / autonomous runs)

- ❌ Do **not** fine-tune the reranker or embeddings (RunPod). 1 success across a
  long failure history; the industry trend is against it; it is not the bottleneck.
- ❌ Do **not** optimize single-shot **recall@10**. It is capped at ~0.77 by task
  size alone (many JIRA tasks change 20-180 files). Retired as a primary metric.
- ❌ Do **not** delete the vector (LanceDB) leg "to simplify" — **measured**, it
  earns +8pp hit@10. It is not baggage.
- ❌ Do **not** trust `MODEL_TRAINING_SPEC.md` / `RERANKER_IMPROVEMENT_PLAN.md` /
  `NEXT_SESSION_PROMPT.md` — superseded, they point the wrong way.

## What was measured (this session)

| Test | Result |
|------|--------|
| Code fixes shipped (commits `22a996b`, `3eebeda`) | hit@10 0.605→0.714, recall@10 0.152→0.182. Env-gated, default ON. |
| Head-to-head, 15 tasks: MCP hybrid (single-shot) vs plain grep-agent (full loop) | ≈ tied. file-recall 0.19 vs 0.18; foothold 0.63 vs 0.51 (hybrid slightly ahead). |
| FTS-only (vector OFF) | hit@10 −8.3pp, recall@pool −7.3pp, retrieval_failures ×2 → **vector earns its keep**. |
| reranker-OFF | _pending — `bench_runs/diagnose/norerank/`; fold result in when done._ |
| Deep research (industry SOTA) | direction = agentic grep-first; but its headline "drop vector = free win" FAILED our test of its own criterion. |

## Decisions LOCKED

- **Reranker fine-tuning: NO.** RunPod money stays parked (not refundable; spend
  only on an off-the-shelf embedding-model **swap bench** or GT cleanup if at all).
- **Primary metric: foothold-recall** (≥1 file per relevant repo in top-K) **+
  steps-to-find.** Not single-shot recall@10.
- **Vector leg: KEEP.** Measured +8pp.
- **Graph + `analyze_task`: KEEP** — repo-routing is the real value (foothold 0.63
  vs single-file 0.19 says the system finds the right *repos* far better than the
  right *files*).
- Kept code fixes (FIX-A/D/F/G/H + provider-doc demotion + daemon-400): committed,
  default ON. Env vars are kill-switches.

## Decision OPEN

**Keep the working hybrid** (just stop chasing FT) **vs full agentic-grep rebuild
(option c)** — replace single-shot retrieval with the agent's own grep-iteration
loop (head-to-head shows a grep agent ≈ the hybrid). Option c is a genuine
re-architecture. Gate the choice on: the reranker-OFF result + a foothold-recall
re-measure.

## Source data

- `bench_runs/diagnose/fixI/` — current hybrid baseline (all fixes, vector+reranker ON)
- `bench_runs/diagnose/ftsonly/` — vector OFF
- `bench_runs/diagnose/norerank/` — reranker OFF
- `bench_runs/headtohead/` — MCP hybrid vs plain grep-agent
- `DEEPRESEARCH_PROMPT.md` — the deep-research brief
- `.claude/autonomous/PROGRESS.md` — full chronological log
