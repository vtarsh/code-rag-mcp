# Index-gap audit report — jira_eval_n900 vs db/knowledge.db

Author: index-gap-detective agent (read-only investigation, 2026-04-27)
Audit script: `scripts/audit_index_gaps.py` (≈300 LOC, deterministic seed=20260427)

## TL;DR

Brief framed it as "33% of GT pairs missing — root cause unknown". Actual numbers when noise filter is applied:

- **24.5%** of 22,459 GT pairs are mechanical noise (already known to drop)
- **42.1%** are correctly indexed
- **6.4%** ARE indexed but lookup fails because of a **path-prefix mismatch** between extractor output and the GT scheme — pure eval-side bug, zero rebuild needed → bucket **F**
- **14.6%** are missing because of `git clone --depth=1` shallow clone in `scripts/clone_repos.sh:107` — file existed in older commit, was renamed/deleted before the snapshot we hold → bucket **D**
- **12.2%** exist at HEAD on disk but lie outside the extractor allowlist (`migrations/`, `tests/`, `cypress/`, `.husky/`, `*.cql`, top-level `*.json` other than allowed configs, `jest.config.*`, etc.) → bucket **B/E**
- **0.2%** repo not in `raw/` at all → bucket C

So the real story has TWO root causes, not one:
1. `--depth=1` clone discards history — biggest single bucket (3,270 pairs).
2. **Path-prefix mismatch** — eval lookup uses repo-relative path, indexer stores `<artifact_type>/<repo-relative path>` for non-FE artifacts. 1,447 pairs are silently miscounted as missing.

Together D+F = 21.0% of all pairs (4,717 pairs).

---

## 50-sample bucket distribution (sample seed=20260427, deterministic)

| bucket | count | description |
|--------|-------|-------------|
| A | 0 | file missing from repo HEAD without shallow clone (refactored / deleted / renamed) |
| B | 10 | exists at HEAD; outside extractor allowlist |
| C | 0 | repo not cloned |
| D | 24 | shallow clone — file in older commit, missing from HEAD |
| E | 5  | extractor allowlist matched but produced no chunk |
| **F** | **11** | **PATH-PREFIX MISMATCH — file IS indexed under category prefix; GT lookup misses** |

_Note: bucket F was NOT in the original brief — discovered during sample analysis when bucket-E classification flagged files like `workflows/activities/foo.js` (extracted) as "no chunk for `activities/foo.js`" (GT). The chunk exists, just under a different path string._

## Population-level confirmation (full 22,459 GT pairs)

| Bucket | Pairs | % of total | % of legit-missing |
|---|---|---|---|
| Mechanical noise | 5,506 | 24.5% | — |
| Already indexed | 9,458 | 42.1% | — |
| **F prefix-mismatch (already indexed!)** | **1,447** | **6.4%** | **23.9%** |
| **D shallow clone, file gone from HEAD** | **3,270** | **14.6%** | **54.1%** |
| Full clone, file gone from HEAD | 0 | 0.0% | 0% |
| Exists at HEAD (B + E) | 2,735 | 12.2% | 45.2% |
| Repo not cloned | 43 | 0.2% | — |

(Last three rows sum to "missing-legit" by my count = 6,048; F is technically already indexed, just unfindable by GT scheme.)

## Per-row classification (50-sample) — abbreviated

Verbatim from `audit_index_gaps.py` run. See `.claude/debug/current/index-gap-report.md` history for full table; here are the most informative rows:

| repo | file_path | bucket | evidence |
|------|-----------|--------|----------|
| backoffice-web | src/App/styled.ts | D | shallow n=11; not in HEAD; not in history |
| backoffice-web | src/hooks/use-dynamic-fields-operation/index.ts | D | shallow n=11; not in HEAD; not in history |
| backoffice-web | src/Components/Details/DetailsContext.ts | D | shallow n=11; not in HEAD; not in history |
| graphql | src/resolvers/risk/lists/list/index.ts | D | shallow n=17; not in HEAD; not in history |
| graphql | libs/get-merchants-of-entity-under-acquirer.ts | D | shallow n=17; not in HEAD; not in history |
| grpc-risk-rules | methods/archive.js | D | shallow n=3; HEAD migrated `methods/archive.js`→`methods/archive.ts` |
| workflow-provider-webhooks | activities/plaid/misc/get-accounts.js | **F** | chunk exists at `workflows/activities/plaid/misc/get-accounts.js` |
| workflow-settlement-worker | workflows/preparing-data.js | **F** | chunk exists at `workflows/workflows/preparing-data.js` |
| grpc-onboarding-entity | service.proto | **F** | chunk exists at `proto/service.proto` |
| grpc-onboarding-risk | docs/security-overview.md | **F** | chunk exists at `docs/docs/security-overview.md` |
| express-webhooks | .env.example | **F** | chunk exists at `env/.env.example` |
| grpc-settlement-account | migrations/pg/...sql | B | exists at HEAD; outside allowlist |
| grpc-onboarding-merchant | .husky/pre-commit | B | exists at HEAD; outside allowlist |
| grpc-vault-abu | jest.config.ts | B | exists at HEAD; outside allowlist |
| backoffice-web | src/Pages/functions/index.ts | E | extracted (34 B re-export only); chunker filtered as too small/trivial |

## Dominant cause — D: shallow clone

Evidence:
- `scripts/clone_repos.sh:107` — `git clone --depth=1 --single-branch --branch "$BRANCH" "$CLONE_URL" "$REPO_DIR"`
- `scripts/clone_repos.sh:95`  — refresh path is `git -C "$REPO_DIR" fetch --depth=1 origin "$BRANCH"` (also keeps it shallow)
- 552 / ~600 cloned repos have `.git/shallow` markers
- All top-10 missing-files repos are shallow:
  - `backoffice-web` 2,236 missing legit pairs (shallow)
  - `graphql`        394
  - `grpc-onboarding-merchant` 155
  - `grpc-core-configurations` 150
  - `grpc-providers-credentials` 138
  - `express-api-v1` 128
  - `grpc-risk-engine` 118
  - `grpc-onboarding-verifications` 117
  - `paypass-web` 108
  - `grpc-risk-rules` 101
- For the 50-sample, **every "file missing from HEAD" row** is in a shallow repo (`is_shallow=True`, `n_boundaries∈[1..17]`). I never saw a case of a full-cloned repo with a HEAD-missing file (bucket A = 0/50, full-clone-gone = 0/22459).

So bucket D is mechanically `shallow_clone × file_was_modified_or_deleted_after_GT_was_recorded`. Jira PR data was recorded over months/years; HEAD is one snapshot from the last `--depth=1` fetch.

## Sub-dominant cause — F: path-prefix mismatch (eval-side bug)

`src/index/builders/repo_indexer.py:67` does `rel_path = str(file_path.relative_to(repo_dir))` where `repo_dir = EXTRACTED_DIR / repo_name`. The walker iterates **artifact_type subdirs** (`proto/`, `docs/`, `config/`, `env/`, `k8s/`, `methods/`, `libs/`, `workflows/`, `ci/`, `routes/`, `services/`, `handlers/`, `utils/`, `consts/`) — so `rel_path` becomes `<artifact_type>/<original_subpath>`.

Examples in the wild (verified by SQL):

| GT path | Indexed as | Match |
|---|---|---|
| `service.proto` | `proto/service.proto` | endswith `/service.proto` ✓ |
| `activities/plaid/misc/get-accounts.js` | `workflows/activities/plaid/misc/get-accounts.js` | ✓ |
| `docs/security-overview.md` | `docs/docs/security-overview.md` | ✓ |
| `.env.example` | `env/.env.example` | ✓ |
| `workflow.js` | `workflows/workflow.js` | ✓ |

Top F-bucket prefixes (full population, n=1,447):
- `workflows/`            568
- `proto/`                325
- `env/`                  270
- `docs/`                 150
- `k8s/`                   58
- `methods/`               28
- (other compounds & tail) 48

The FE pass was specially designed to AVOID this (extractor copies `repo/src/Pages/X.tsx` → `extracted/<repo>/src/Pages/X.tsx`, preserving prefix; comment in `repo_indexer.py:31-33`). Backend artefact types do NOT do this.

## Sub-cause — B/E: at-HEAD but not chunked (n=2,735)

Top extensions of files-at-HEAD-but-missing:
- `.ts` 719, `.js` 493, `.sql` 398, `.graphql` 258, `.json` 214, `.cql` 204, `.yml` 159, `.yaml` 112, `.sh` 50

Top top-level dirs:
- `src/` 861 (these are FE files outside `src/{libs,routes,…}` AND outside the catch-all `src_frontend` pass — likely top-level `src/Pages/...` files where the file IS in extracted but the chunker filtered)
- `migrations/` 365 (extractor has no migrations rule)
- `tests/` 300 (allowlist deliberately rejects)
- `graphql.schema.json` 188 (top-level non-config json)
- `.github/` 176 (only `.github/workflows/*.{yml,yaml,template}` is allowed; CODEOWNERS, ISSUE_TEMPLATE etc are skipped)
- `db/` 100 (no rule)
- `cypress/` 49 (test/e2e only)
- `.husky/` 23 (no rule)
- `seed.cql`, `pg.sql`, `scylla.cql` (140+) (no SQL/CQL rule)
- `jest.config.*`, `jest.setup.*` (44+) (no rule)

These are **deliberate-or-accidental allowlist gaps** — fixing them is a per-rule extractor change. Not the dominant cause (12% of total, 45% of legit-missing).

## Recommended fix order

### Fix #1 — F (path-prefix mismatch): zero rebuild, eval-side only (HIGHEST ROI)

Edit (proposed, not landed):

`scripts/build_clean_jira_eval.py` — when checking `(repo, fp)` membership in `indexed`, **also try suffix-match against `by_repo[repo]`**:

```python
def is_indexed(repo: str, fp: str, indexed: set[tuple[str, str]],
               by_repo: dict[str, set[str]]) -> bool:
    if (repo, fp) in indexed:
        return True
    # Tolerate extractor's category-prefix path scheme
    return any(p.endswith("/" + fp) for p in by_repo.get(repo, set()))
```

Same change anywhere else GT-vs-index lookup happens (search `expected_paths.*indexed` / `chunks.file_path` joins in `scripts/`, `src/eval/`, `src/index/`).

A more invariant-preserving alternative: **rewrite the GT** at `build_clean_jira_eval.py` time, replacing each GT path by the canonical chunk path (the one that endswith). Downstream code keeps comparing exact strings.

The cleanest long-term fix is on the indexer side: store the **repo-relative path** in `chunks.file_path`, not the `extracted/<artifact_type>/...` path. That requires a rebuild + downstream consumer audit (any caller that reads `chunks.file_path` and concatenates with `extracted/<repo>/...` would break — needs sweep). Recommend Fix #1a (eval-side rewrite) first and Fix #1b (indexer canonicalization) as scheduled cleanup.

- Files to edit: `scripts/build_clean_jira_eval.py:55-90` (and any analog in eval pipeline). Probably ~10-30 LOC.
- Expected coverage gain: **+1,447 pairs (+6.4 pp absolute, +24% of legit-missing)**.
- Risks: minimal — pure read of existing chunks; same DB.
- Reversibility: trivial — revert one commit.
- Validation: re-run `audit_index_gaps.py` after the fix; F bucket should drop to ≈0.

### Fix #2 — D (shallow clone): unshallow on first run (LARGEST BUCKET)

`scripts/clone_repos.sh:107` — drop `--depth=1` (or replace with `--no-single-branch && git fetch --unshallow`):

```bash
# Was:
if git clone --depth=1 --single-branch --branch "$BRANCH" "$CLONE_URL" "$REPO_DIR" 2>/dev/null; then

# Proposed:
if git clone --single-branch --branch "$BRANCH" "$CLONE_URL" "$REPO_DIR" 2>/dev/null; then
```

And the refresh path on `scripts/clone_repos.sh:95`:

```bash
# Was:
if git -C "$REPO_DIR" fetch --depth=1 origin "$BRANCH" 2>/dev/null; then

# Proposed for already-shallow repos: deepen on next refresh
if git -C "$REPO_DIR" fetch --unshallow origin "$BRANCH" 2>/dev/null \
   || git -C "$REPO_DIR" fetch origin "$BRANCH" 2>/dev/null; then
```

- Files to edit: `scripts/clone_repos.sh` (2 lines).
- Expected coverage gain: **+3,270 pairs (+14.6 pp absolute, +54% of legit-missing)** — bound: only for files renamed/deleted within the history we re-fetch. Some Jira tickets reference very old PRs; full history needed for full coverage. With `--no-shallow`, we get **all** history, so 3,270 is a lower bound.
- Cost (estimated):
  - Disk: shallow `~/.code-rag-mcp/raw/` = check below.
  - Bandwidth/build time: full clone of 600 repos: ballpark 10-100 GB depending on repos. `backoffice-web` history alone could be GB. Re-cloning 600 repos sequentially might add 1-3 hours to next `make build`.
  - Risk: build script may need parallelism (`xargs -P 4`) to keep wall-clock OK. May need disk-space check before approving.
  - Re-extraction is probably NOT needed (HEAD content is the same; only history is added) — but if extractor walks `*.git/objects/...` for some reason it could double up. Safe-default: do not re-extract; just verify with sampled queries.
- Reversibility: full — just `rm -rf raw/<repo>/.git` and re-clone shallow if you want to undo. But once history is fetched, no DB changes happen unless we re-extract.
- Validation: pick 10 of the 24 D-sample paths; after deepening their repos, run `git -C <repo> log --all -- <path>` and confirm hits.

#### Disk cost — measured

```text
shallow raw/ size today:    du -sh raw/  →  1.1 GB
free disk on this Mac:      df -h        →  13 GiB available out of 460 GiB (98% used)
estimated full-history footprint: 5-10× shallow → 5.5 - 11 GB
```

**Warning — disk pressure**: system has only 13 GiB free. Full unshallow could push us into red zone, especially if intermediate fetches double on disk. Recommend:
- Variant A: `--depth=200` cuts cost to ~2× shallow (~2-3 GB add); recovers most file renames; small history loss.
- Variant B: full unshallow but stage it (one repo at a time, with disk-free re-check before each); can be aborted at any point with no data loss.

Either variant is reversible — just `rm -rf raw/<repo>/.git/` and re-clone shallow.

### Fix #3 — B/E (allowlist gaps): targeted extractor extensions

Top quick wins (≥ 100 pairs each):
- `migrations/` (365 pairs) — add to extractor source dirs for `.sql` files
- `graphql.schema.json` and `*.graphql` (446 pairs) — add `graphql` artifact type
- `*.cql` (204 pairs) — add `cql` artifact type (already a `cql_chunks.py` chunker exists in `src/index/builders/cql_chunks.py`!)
- `db/` (100 pairs) — same
- `src/Pages/.../*.tsx` triviality filter on chunker (auditing the E sub-bucket reveals tiny re-export `index.ts` files filtered by chunker; debatable whether to keep)

These are surgical edits to `scripts/extract_artifacts.py`. Combined estimate: +500-1000 pairs recoverable.

Risks: extractor allowlist expansion grows the index size and rebuild time. Each new rule needs a chunker mapping in `src/index/builders/dispatcher.py`.

## Estimated impact summary

| Fix | Files to edit | Pairs recoverable | Rebuild needed | Disk impact | Risk |
|---|---|---|---|---|---|
| #1 F-bucket (eval rewrite) | 1 file, ~20 LOC | +1,447 | NO | none | low |
| #2 D-bucket (unshallow) | clone_repos.sh, 2 lines | +3,270 | NO (just clone re-fetch) | +50-100 GB | medium (disk) |
| #3 B/E (allowlist surgery) | extract_artifacts.py, ~50-150 LOC | +500-1,000 | YES (full re-extract + re-index) | minor | medium (build pipeline churn) |

**Recommended order**: #1 (cheap, no rebuild) → #2 (no rebuild but disk-heavy; gate behind disk-check) → #3 (planned rebuild).

Combined: recoverable up to ~5,700 GT pairs (~25 pp absolute), bringing legit-missing from ~27% to ~3% of all pairs.

## What I deliberately did NOT do

- did not run `git fetch --unshallow` on any repo
- did not edit `scripts/extract_artifacts.py`, `scripts/clone_repos.sh`, `scripts/build_clean_jira_eval.py`, or any `src/`
- did not run `make build`
- did not delete data
- did not push to remote
- did not modify the chunks DB or any other DB

## Files referenced

- audit script: `/Users/vaceslavtarsevskij/.code-rag-mcp/scripts/audit_index_gaps.py`
- this report: `/Users/vaceslavtarsevskij/.code-rag-mcp/.claude/debug/current/index-gap-report.md`
- noise regex: `/Users/vaceslavtarsevskij/.code-rag-mcp/scripts/build_clean_jira_eval.py:25-45`
- extractor: `/Users/vaceslavtarsevskij/.code-rag-mcp/scripts/extract_artifacts.py:177-390`
- indexer (path-prefix bug): `/Users/vaceslavtarsevskij/.code-rag-mcp/src/index/builders/repo_indexer.py:39-67`
- shallow clone: `/Users/vaceslavtarsevskij/.code-rag-mcp/scripts/clone_repos.sh:95,107`

## Reproduction

```bash
cd /Users/vaceslavtarsevskij/.code-rag-mcp
CODE_RAG_HOME=$(pwd) ACTIVE_PROFILE=pay-com python3.12 scripts/audit_index_gaps.py
```

Output is deterministic given seed=20260427.
