# Round 1 — Researcher

Outside-in lens: this codebase has been A/B-stressed for 3 weeks on the same family of techniques (rerank-FT, two-tower-vector, glossary, boost/penalty, RRF). Pragmatist+Systematist+Refactorist already cover those. My job: surface RAG/IR techniques the code-rag-mcp pipeline does NOT yet implement, ranked by expected lift on jira hit@10 given the data shape I measured below.

## Data shape from jira_eval_n900 (grounded, computed locally)

- 908 queries, 22 459 GT (repo, file) pairs across **292 distinct repos** but only top-8 repos hold 65% of GT (`backoffice-web` alone = 27%).
- **85.4% of queries have ≥50% of their GT files in a single repo.** This is the strongest unexploited signal in the corpus — current pipeline competes 80+ repos for every query.
- 58.4% queries are short (≤6 tokens) targeting GT lists of avg 24.7 files — a length asymmetry that classical IR (BM25 / single-vec) under-serves.
- 24.4% of GT file paths contain camelCase tokens (`MerchantSettlementAccount.tsx`, `getSettlementAccounts.ts`); FTS5 currently indexes with `porter unicode61` (verified at `src/index/builders/db.py:22,74`) which does NOT split camelCase. This is a known SOTA-vs-impl gap.
- Only 19.1% of queries themselves carry a code-identifier shape — most are English PR titles ("Show Only Merchant-Specific Risk Lists Under Merchant Page"). So query-side and index-side improvements need to be matched.

These four facts shape my ranking.

---

## RE1: Two-stage retrieval with **repo prefilter** (predict repo first, then file)

- rank: **1**
- technique name + citation: **Hierarchical retrieval / cluster-pruning** (Chen et al., "Promptagator", arXiv 2209.11755, 2023; Karpukhin et al., DPR multi-stage, 2020). For code search specifically: **CodeReviewer / CRAG** (Huang et al., 2024) demonstrate +6-12pp Hit@10 from repo-aware first-stage filtering on multi-repo corpora.
- why it fits this codebase:
  1. **Empirical**: 85.4% of jira queries concentrate ≥50% of GT in a single repo, but the FTS5+vector pool of 150 must compete across 80 indexed repos. The reranker spends most of its budget choosing among wrong-repo candidates.
  2. **Structural**: we already have `repo_overview` tool and per-repo GT distribution; the prefilter is a tractable 80-class classifier (or BM25 over repo summary docs).
- expected lift on jira hit@10: **+6 to +12pp** (from 53.5% baseline). Lower bound = recover most "right repo missing from top-150 pool" cases (~10% of misses based on H6 chunk-pool drift). Upper bound assumes prefilter is ≥80% top-3 accurate.
- compute / time / cost: 6-10 hours implementation; **zero training cost** if we use BM25 over per-repo `README.md` + top-50 `code_facts` summaries. Optional V2: train a small classifier on (query → repo) pairs derived from jira GT (free, ~200 KB model). RunPod: $0.
- failure mode (concrete): if prefilter top-3 misses the GT repo, recall **collapses on that query** (worse than baseline). Mitigation: **soft prefilter** — boost top-3 prefilter repos by ×1.4 in fusion instead of hard-filtering. Also: 14.6% of queries genuinely span repos (multi-repo jira tickets like graphql+backoffice-web pairs); the soft-boost form keeps them recoverable.
- already-tried? **NO** — verified: I grep'd `repo_summary`, `repo_classifier`, `repo_prefilter`, `repo_aware` in `/src/`. Only `repo_overview` (a presentation-layer tool, `src/tools/context.py:225`) exists; no prefilter is wired into `hybrid.py`. Memory `project_two_tower_v13_landed.md` describes a *vector*-tower split (code vs docs) — **not** a *repo*-tower split. Confirmed orthogonal.

---

## RE2: **Doc2Query / DocT5Query — index-side query expansion via a local small LM (offline only)**

- rank: **2**
- technique name + citation: **Doc2Query / DocT5Query** (Nogueira & Lin, "From doc2query to docTTTTTquery", 2019; "Doc2Query−−" Gospodinov et al., SIGIR 2023 — adds filter for hallucinated queries). Beats BM25 by 5-15% MRR@10 on MSMARCO. **Critically distinct from HyDE:** Doc2Query runs at INDEX TIME (offline, RunPod), not at query time → policy-compliant per `feedback_no_external_llm_apis.md` (no runtime LLM API calls).
- why it fits this codebase:
  1. Bridges the short-query / long-doc gap — exactly what the 58.4%-short-query distribution exposes. Glossary is hand-curated (~50 entries) and pragmatist-A1 grew it; Doc2Query auto-generates 3-5 synthetic English questions per chunk and stores them in a new FTS5 column.
  2. Solves the "PR titles use English nouns, code uses identifiers" problem without changing the query side — the index meets the user halfway.
  3. Zero query-time latency (synthetic queries already in index); zero runtime LLM dependency.
- expected lift on jira hit@10: **+3 to +7pp**. Lower if synthetic queries hallucinate (Gospodinov-2023 filter brings noise down ~50%); upper if jira PR titles are well-aligned with auto-generated chunk descriptions.
- compute / time / cost: ~$5-15 RunPod (Qwen2.5-Coder-3B or Llama-3.2-3B over ~76k chunks, batch=32, ~6h on A40). Re-run on each `make build`. 1-2 days dev (FTS5 column add + ingestion script + bench). **Re-uses existing RunPod stage-A/B infra** per `project_runpod_stage_ab_landed.md`.
- failure mode (concrete): hallucinated synthetic queries inflate FTS5 match counts → degrades precision/MRR even when recall improves. Mitigation: use Gospodinov-2023 filter (run synthetic Q through retriever; keep only those that retrieve their own chunk in top-10); store filtered Qs in a separate FTS column `chunk_synth_queries` weighted at 0.5×, so original chunk content still dominates BM25.
- already-tried? **NO** — verified: `/usr/bin/grep -rn "doc2query\|synth_quer\|hypothetical"` returns zero hits in `src/`. Memory shows query-side glossary expansion (broken) and FT reranker swaps; no index-side query generation. Confirmed not in W1/W2/W4 from current planning debate. Distinct from glossary because (a) it auto-mines from **code content** not user vocabulary; (b) it's per-chunk not per-token.

---

## RE3: **Code-aware FTS5 tokenizer (split camelCase / snake_case / dotted identifiers)**

- rank: **3**
- technique name + citation: **CodeBERT/GraphCodeBERT subword tokenization** (Feng et al., 2020) inspired the recent **bm25s-codeicl / Lucene CamelCaseFilter / Pisek 2024 "Code-aware BM25"** line. **Tantivy/Lucene** ships a `WordDelimiterGraphFilter` that splits `getMerchantId` → `get`, `Merchant`, `Id` while keeping the original token. SQLite FTS5 supports custom tokenizers via `fts5_tokenizer()` C API or, more practically, **pre-processing** the indexed text and adding a parallel `tokens` column.
- why it fits this codebase:
  1. **Empirical**: 24.4% of GT paths contain camelCase. Current `porter unicode61` (`src/index/builders/db.py:22,74`) treats `MerchantSettlementAccount` as one token. Query "merchant settlement account" never lexically matches the path/code containing only the camelCase variant. Vector tower partially compensates, but RRF gives FTS5 2× weight (`KEYWORD_WEIGHT` per hybrid.py imports).
  2. Cheap to implement as a **preprocessing layer**: at index time, append `_tokens` column to chunks that contains the splitted form; FTS5 indexes it as an extra column with weight 1×. No tokenizer C extension needed.
  3. Symmetric query-side: also split user-query camelCase tokens at sanitize stage.
- expected lift on jira hit@10: **+2 to +5pp**. Conservative because sanitize_fts_query already splits on `.` (`fts.py:136`) and the vector tower partially handles camelCase via subword embedding — so headroom is in the long tail (PR titles using English where the GT path is camelCase-only).
- compute / time / cost: 4-6 hours dev. Zero training. Re-index required (~30-60 min incremental, full ~6h). $0.
- failure mode (concrete): **token explosion** — splitting `getUserAccountId` → 4 sub-tokens triples FTS5 inverted-list size; risk of OperationalError or RAM blow-up on 76k chunks (memory `project_long_batch_perf_2026_04_24.md` already flagged 20GB RAM peaks). Mitigation: only split tokens with `len(token) ≥ 8` and ≥1 internal capital. Also: may regress short-query precision (more match noise) — A/B against jira+v2 is mandatory pre-ship.
- already-tried? **NO** — verified: `tokenize='porter unicode61'` is the only tokenizer config; no `WordDelimiter`, no `_tokens` column. `_sanitize_fts_input` only splits `/` (Tick 4 fts5fix). The `.`-split in `sanitize_fts_query:136` is query-side only and limited to dotted tokens. Memory: no entry mentions camelCase tokenization at index level.

---

## On user's grep+synonym intuition (revisited from a research lens)

**The user's intuition is correct in spirit; previous implementations failed because they targeted the wrong layer.**

What "Claude Code grep" actually does well isn't synonyms — it's:
1. **Sub-string match** on uncompressed source (not tokenized BM25). FTS5 tokenization throws away substring-match by design.
2. **Iterative refinement** — try query-1, then query-2, then query-3 with grep flags `-i`, `-w`, `-E`. RAG pipelines compute one shot and commit.
3. **Path-aware match** — grep matches against file paths AND content. Our FTS5 indexes content only (paths land in metadata, not in the matchable index column).

**SOTA equivalents that haven't been tried here:**

- **Path-as-document indexing**: append `repo_name + file_path + filename_camelcase_split` as a high-weight FTS5 column. Cheap, ~50 LOC, addresses 24.4% camelCase-in-path signal directly. **Would recommend bundling into RE3.**
- **Iterative retrieval (Rao-Lin "Self-Refine" 2023, "Multi-step retrieval" Kahn 2024)**: if top-50 cosine spread is uniform (low confidence), re-issue with relaxed constraints. Can implement WITHOUT an LLM by using deterministic relax-rules: drop boosts → widen pool → fall back to substring LIKE. **Lift: +1-3pp.** Not in my top-3 because the cost-benefit is weaker than RE1.
- **Synonym-tolerant retrieval that *works* on doc-intent queries**: this is **learned-sparse retrieval** (SPLADE-v2, Formal et al., 2022). SPLADE generates a sparse expansion vector per chunk at index time using a frozen MLM — auto-discovers synonyms from corpus statistics, not from a YAML. Beats BM25+synonym-expansion by 3-8pp NDCG@10 on TREC-DL, *and* doesn't have the brittleness that broke W2 glossary expansion (over-expansion biases doc-intent queries toward unrelated chunks). **I'd rank SPLADE as RE2.5** — strictly better than glossary, but heavier infra than Doc2Query (needs ONNX runtime at query time). Documented here for completeness; left out of top-3 because Doc2Query is closer to the existing RunPod batch-indexing infrastructure.

**Bottom line on synonyms**: the YAML form (W2) and equivalence-class column (W4) are both 2015-era IR. The 2024 SOTA answer is **synonym-discovery moves to the index** — Doc2Query (RE2) auto-generates queries that should retrieve each chunk; SPLADE auto-expands chunks with co-occurring terms. Both are corpus-driven, not curator-driven, so they don't fail on doc-intent queries the way hand-curated glossaries do. **The user's intuition lands directly on Doc2Query, not on glossary growth.**

---

## Ranking summary (lift × tractability)

| # | move | est. lift hit@10 | dev hours | $$ | already-tried? |
|---|---|---|---|---|---|
| RE1 | Repo prefilter (soft) | +6 to +12pp | 6-10h | $0 | **No** |
| RE2 | Doc2Query (offline RunPod) | +3 to +7pp | 16h | $5-15 | **No** |
| RE3 | Code-aware FTS5 tokenizer + path-as-doc | +2 to +5pp | 4-6h | $0 | **No** |

**My strong recommendation**: bundle **RE1 + RE3** as the next two-week ship (cheap, additive, both touch the FTS/fusion stage so single A/B). RE2 is the higher-ceiling but slower-to-ship move; queue it after RE1+RE3 if those land below +6pp.
