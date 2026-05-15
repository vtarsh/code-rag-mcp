# Round 1 — Data Engineer

> Prior: data quality is upstream. The eval-set, GT, and index pipeline are noisier than any rerank/ranking knob will compensate for. Three biggest wins are all DATA moves; ranker work without these is sandcastle.

---

## DE1: Rebuild eval against ACTUALLY-INDEXED HEAD — drop unhittable + GT-noise rows

- **rank**: 1
- **data-quality framing**: violates the basic invariant **GT ⊆ index**. Today the `jira_eval_n900` eval contains GT pairs from PR diffs whose paths NO LONGER EXIST in the current shallow-cloned HEAD. Verified: `backoffice-web/src/Components/ContactList/ContactList.tsx` cited by BO-1041 — directory is gone in current HEAD; `.git/shallow` shows 1 visible commit. We are scoring a retriever against PR-archeology paths that have been refactored out of the codebase. **42.1% (9458/22459) of GT pairs are indexed; 25.4% (5696/22459) of GT pairs are mechanical PR-noise** (`package-lock.json` 1141×, `package.json` 1025×, `consts.js` 237×, `src/generated/graphql.ts` 214×, `.drone.yml` 105×, `Dockerfile` 53×, `tests/index.spec.js` 53×, `__generated__/`, `.eslintrc`, etc.). 86 queries (9.5%) have **ZERO indexed expected paths** and are mathematically unhittable — they pollute hit@10 with -9.5pp baseline floor.
- **justification (2 sentences)**: When 57.9% of "ground truth" is missing from the corpus and another 25.4% is package-lock-style noise, hit@10 is 18% measuring retrieval quality and 82% measuring index/eval drift. Every percentage point we "win" by tuning the ranker against this eval is half-noise — the W2 glossary regression (-19.83pp v2) and W1 noise (+0.77pp jira) over the past sessions are direct consequences of optimizing against a metric whose signal-to-noise ratio is sub-1.
- **failure mode (1 concrete way it could fail)**: A clean rebuilt eval may collapse to ≈400-500 queries (after dropping 86 fully-unindexed + ~50 noise-only + queries where remaining clean GT < 3). The smaller eval may have less statistical power, especially per-stratum. Mitigation: keep the n=908 set as a "frozen historical regression eval" but make a NEW `jira_eval_clean_n450` the primary optimization target. Track BOTH; promote a candidate only if both move in the same direction (or only-clean moves, only-historical doesn't regress significantly).
- **compute / time cost**: ~4 hours. (1) Script: drop GT pairs not in `chunks` table; drop noise patterns; drop queries with <3 remaining clean pairs. (2) Re-bench `wide_off_session2` baseline + `fts5fix` candidate on the new eval. (3) Recompute bootstrap CI. No GPU. ~150 LOC of Python + 1 jira bench rerun.

---

## DE2: Index re-build with relaxed extractor — cover `.sql / .graphql / .json / .yml / .cql / .proto` GT files

- **rank**: 2
- **data-quality framing**: violates the **representativeness contract** — the chunker is an explicit allowlist over directory names (`src/`, `packages/*/src/`, `apps/*/src/`, `libs/`, `routes/`, `services/`, `handlers/`, `utils/`, `consts/`, `methods/`) and an explicit allowlist of extensions (`.ts/.tsx/.js/.jsx/.mjs/.go/.proto/.md`). Several extensions that ARE legitimate retrieval targets are 0% indexed: `.json` 0/2506 (graphql.schema.json, package.json, package-lock.json — first two ARE legit GT), `.yml` 0/357, `.proto` 0/325 (schema definitions! — wait, .proto IS extracted, but nested `*.proto` files outside repo root are missed → check), `.sql` 0/492, `.graphql` 0/263, `.cql` 6%. Fixing these would lift the **R@10 ceiling** from 39.55% to ~50-55% and unblock 86 zero-indexed queries.
- **justification (2 sentences)**: The recall@10 numerator is bounded by what's in the index, not by the ranker. Making the GT denominator more honest (DE1) plus increasing the indexed numerator (DE2) is the only way to break the 60% jira hit@10 ceiling that two months of ranking-layer tweaks have plateaued at.
- **failure mode (1 concrete way it could fail)**: Adding raw `.json` / `.yml` / generated `.ts` files to the corpus will **double-count and dilute** the FTS5 + vector pools. Provider doc dominance (IR3 in independent.md: 56% of chunks already are docs) gets worse — code GT may be displaced by `package.json` + schema.json + helm values. Mitigation: index these extensions but with a **negative document-type prior** (e.g., `lockfile_penalty=0.05`, `schema_penalty=0.20`, applied unconditionally regardless of `_query_wants_docs`). Better: index them in a SEPARATE FTS table and only consult on direct repo+filename query, not on default fanout.
- **compute / time cost**: ~6-8 hours human + 2-3 hours machine. (1) Edit `scripts/extract_artifacts.py:60-90`: add `.sql/.graphql/.cql/.yml` allowlists with category-specific dirs (`db/`, `migrations/`, `schema/`, `helm/`, `deploy/`). (2) Decide noise extensions to keep OUT (`.eslintrc`, `.prettierignore`, `.dockerignore`, `.gitignore`). (3) Rebuild incremental: `make build` is ~30-60 min on changed repos only. (4) Validate: re-bench wide-OFF on jira; expect +R@10 ≥3pp on indexable strata, neutral on doc-intent.

---

## DE3: Pivot primary eval to `tool_calls.jsonl` real prod queries — jira-titles distribution is wrong

- **rank**: 3
- **data-quality framing**: violates the **target-distribution contract** — the question we're optimizing for (jira PR titles) does NOT match the question users ask. Measured on `logs/tool_calls.jsonl` (3285 real search queries vs jira n=908):

| metric | prod queries | jira eval | delta |
|---|---|---|---|
| ≤3 tokens | 39.6% | 10.8% | +28.8pp |
| code-shape signature (camelCase / snake_case / `.ts` / UPPERCASE) | 30.2% | 23.5% | +6.7pp |
| medium length 4-8 tokens | 50.9% | 71.9% | -21.0pp |
| long >8 tokens | 9.5% | 17.3% | -7.8pp |

Real prod queries look like `trustly verification webhook`, `doNotExpire APM session workflow`, `express-api-v1 call-providers-initialize doNotExpire okto nuvei` — short, code-token-laden, debugging-style. Jira queries look like `Refactor update merchant and merchant application`, `Settlement Fixes - Days, refresh, options` — prose, vague-PR-title. **The two are different distributions**, and eval-v2 (n=161 docs) was already shown to invert verdicts vs eval-v1. There is no reason to assume optimizing for jira hit@10 generalizes to prod.

- **justification (2 sentences)**: We've spent two sessions optimizing for jira-titles and the trade-offs we keep finding (W2 helps jira -19.83pp on v2; narrow-OFF wins jira +0.66pp loses v2 -6.85pp) are symptoms of optimizing for the wrong distribution. A `prod_eval_n500` derived from `tool_calls.jsonl` clusters + manual labeling of GT (which file did the user ACTUALLY click into / read after?) would be a metric that, if moved, moves the user-facing experience.
- **failure mode (1 concrete way it could fail)**: `tool_calls.jsonl` doesn't carry click/follow-up signal — we have queries but no GT labels for "which result was the right one". Deriving GT requires either (a) joining tool_calls with subsequent `Read`/`Edit` tool calls in the same session within ~30s window (proxy-label, weak signal) or (b) Opus + MiniLM dual-judge labeling like eval-v3 (~$5-10 in API and 6h human review). Without GT we cannot compute hit@k. Mitigation: start with proxy-label (read-after-search heuristic), validate on the 161 v2 calibrated overlap, then expand.
- **compute / time cost**: ~12 hours human + ~$10 API + ~30 minutes bench. (1) Cluster prod queries to ~500 representative seeds. (2) Mine session windows for read/edit follow-ups → proxy GT. (3) Sanity-check vs Opus judge on a 50-query sample. (4) Add as `bench_runs/prod_e2e_*` alongside jira + v2.

---

## On any "tweak ranker" proposal you'd reject

- **VETO category 1 — boost/penalty/threshold tuning** (`gotchas_boost`, `DOC_PENALTY`, `KEYWORD_WEIGHT`, `CODE_FACT_BOOST`, `RRF_K`, `rerank_pool_size`). 6 sessions of evidence: every knob has been swept, every "win" has been ±3pp noise, and the W1/W2/D1 prior round just added more debt. Reason: these are all post-retrieval re-shuffling. With 57.9% of GT not in the index and 25.4% of GT being mechanical noise, the upstream Shannon information bound on what the ranker can recover is < 60% hit@10. Knob-tweaking against a noisy eval is rotation in noise space — the gradient is dominated by GT-noise rather than ranking-quality.

- **VETO category 2 — model swap or fine-tune cycles** (CodeRankEmbed → nomic-v2-moe / gte-large / mxbai-rerank-v3, two-tower → three-tower, train new reranker on jira pairs). 5 sessions of evidence: payfin-v0/v1-fixed -10.8/-8.3pp, nomic-v2-moe -4.1pp, mxbai latency-breach, FT reranker l12 ships +3.31pp on code but cross-domain transfer fails on docs (P9). Reason: no encoder/reranker improvement can compensate for **GT pointing at a deleted file**. Fine-tuning on biased eval pairs encodes the bias into model weights — even more expensive to undo than a bench tweak. Model work belongs AFTER the eval is clean, not before.

---

## Synthesis lens

The three moves above are sequenced, NOT alternatives:

1. **DE1 (eval clean)** — 4h; unblocks honest measurement; tells us the REAL gap from clean baseline to 60% hit@10.
2. **DE2 (index relax)** — 8h + 3h compute; lifts R@10 ceiling; only valuable if DE1 first (otherwise we're indexing noise-targets).
3. **DE3 (prod eval)** — 12h + $10; builds the metric we should have been optimizing all along; needed before any further model/ranker investment.

If forced to pick one for this debate: **DE1**. Cheapest, fastest, exposes whether the 53.5% post-FTS5-fix is a real ceiling or an artifact. Without DE1, every subsequent number is built on a foundation that's measurably 25-58% noise.
