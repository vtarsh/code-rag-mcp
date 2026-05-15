# AGENTS.md Maintenance Rules

> Schema layer for keeping the AGENTS.md navigation catalog accurate as the repo evolves.
> When you modify file structure, read this rule first, then update the catalog before finishing.

---

## When to Update AGENTS.md

Update **root** `AGENTS.md` and/or **relevant child** `AGENTS.md` files when:

1. **Adding a new top-level directory** — e.g. `deploy/`, `docs-gen/`, `openapi/`. Add to the root Directory Tree and Storage Classification.
2. **Adding 3+ new files in an existing directory** — e.g. three new builder modules in `src/graph/builders/`. Update the module list, the explicit count, and the appendix if present.
3. **Renaming or moving a file/directory that is referenced in AGENTS.md** — e.g. renaming `src/search/hybrid.py` → `src/search/router.py`. Update every inline link, table row, and mention.
4. **Changing storage classification** — e.g. promoting a generated artifact directory to git-tracked, or vice versa. Update the Storage Classification table and the git status badge.
5. **Adding or removing entry points** — e.g. a new CLI (`cli_v2.py`), a new server, a new Makefile target. Update the Entry Points section and the Navigable Index.
6. **Adding a new AGENTS.md file** — e.g. creating `profiles/new-org/AGENTS.md`. Add it to the root Navigable Index and the Child Catalogs list in `## Backlinks`.
7. **Removing an AGENTS.md file** — remove all `[[...]]` references to it across every AGENTS.md that mentions it, and purge from `## Backlinks` sections.

---

## What NOT to Update AGENTS.md For

Skip the catalog update when:

- **Single file addition** in an existing well-documented directory (e.g. one new helper in `src/tools/analyze/`).
- **Code changes that don't affect file structure** — refactors, bug fixes, feature additions inside existing files.
- **Temporary/debug files** — e.g. `.claude/debug/session-2026-05-16.md`.
- **Test files** unless they add a whole new test category (e.g. a new `test_load_balancer.py` does not require an update; adding 5 new files under `tests/integration/k8s/` might).

---

## How to Update

### 1. Read Before Writing

Read **both** (or all) affected `AGENTS.md` files before making changes:

```bash
# Example: modifying src/graph/builders/
cat AGENTS.md                          # root catalog
cat profiles/pay-com/AGENTS.md         # if pay-com builders are referenced
```

### 2. Keep Backlinks Style Consistent

Use the exact syntax conventions already in the repo:

- **Inter-AGENTS links** (cross-catalog navigation): `[[path/to/AGENTS.md|Display Name]]`
  - Example: `[[profiles/pay-com/AGENTS.md|Pay-Com Profile Catalog]]`
- **Code / file links** (pointing to source files): `[Display Name](relative/path)`
  - Example: `[src/search/hybrid.py](src/search/hybrid.py)`

Do not introduce new wiki-link styles or absolute `file://` URLs.

### 3. Update Counts Explicitly

Every quantitative claim must be a literal number. Do **not** use vague phrases like "~N files" or "dozens" unless the exact count is genuinely volatile and noted as approximate.

| Bad | Good |
|-----|------|
| "several builder modules" | "14 modules" |
| "~60 tests" | "62 test files + conftest.py + __init__.py" |
| "many scripts" | "~116 scripts + helpers" |

When adding one module to `src/graph/builders/`, change `(14 modules)` → `(15 modules)` and append the new file to the comma-separated appendix list.

### 4. Run the Health Check

After every AGENTS.md modification, run:

```bash
python scripts/health_check_agents_md.py
```

If the script does not exist, validate manually:

- Every `[...](...)` link resolves to an existing file.
- Every `[[...|...]]` link resolves to an existing AGENTS.md.
- Counts in prose match counts in lists / trees.
- Storage classification badges match `git check-ignore` or `git ls-files` reality.

Fix broken links and count mismatches **immediately** — do not leave them for a later session.

### 5. Do Not Commit Without Human Review

AGENTS.md files are high-trust navigation contracts. Stage them, but let a human review before commit.

---

## Directory-Specific Rules

### `src/`

- **New modules:** Enumerate them in the root Directory Tree under the correct sub-package. If the sub-package does not exist yet, create its tree entry.
- **Builder counts:** If adding a builder module to `src/graph/builders/` or `src/index/builders/`, update both the inline count in the tree and the `## Appendix: Builder Modules` comma-separated list.
- **New sub-packages:** If adding a new top-level package under `src/` (e.g. `src/load_balancer/`), add it to the tree, add a one-line description, and add an anchor link in the Navigable Index.

**Example:** Adding `src/graph/builders/redis_edges.py`:

```markdown
# In root AGENTS.md Directory Tree
├── graph/
│   └── builders/         edge builders (15 modules; see appendix below)

# In Appendix: Builder Modules
### `src/graph/builders/` (15 modules)
[...existing list...], [`redis_edges.py`](src/graph/builders/redis_edges.py)
```

### `scripts/`

- **Add to the appropriate group** in the Scripts Catalog (Build Pipeline, Benchmarking, Analysis, ML, Utilities, etc.).
- **Update the total count** in the Directory Tree line (`~116 scripts + helpers` → `~117 scripts + helpers`).
- **Note git status:** If the script is gitignored or untracked, append `(gitignored)` or `(untracked)` in the description or table.

**Example:** Adding `scripts/health_check_agents_md.py`:

```markdown
| [`scripts/health_check_agents_md.py`](scripts/health_check_agents_md.py) | Validation | Lints AGENTS.md links, counts, and classification consistency. |
```

### `tests/`

- **Add to the correct test group** description in `## Tests Structure`. If the file does not fit an existing group, create a new group row.
- **Update the total count** in the Directory Tree (`62 test files` → `63 test files`).
- **Do NOT update** for a single new test file in an existing group unless the user explicitly asks for catalog freshness.

**Example:** Adding `tests/test_graph_redis.py`:

```markdown
| **Graph** | `test_graph_queries.py`, `test_graph_redis.py` | BFS, path-finding, Redis edge store |
```

### `profiles/pay-com/docs/`

Curated docs are tightly catalogued. Every addition or removal requires updating the relevant subsection:

- **Gotchas:** Add the file to the correct sub-table (cross-cutting / per-provider / credentials) with a 1-line summary.
- **References:** Add to the correct semantic sub-section (Setup, Architecture, Contracts, etc.) with a 1-line summary.
- **Flows:** Add to the `docs/flows/` table with a brief description.
- **Notes / MOC:** Add to the MOC table.
- **Update counts** in the Backlinks Index and section headers (e.g. "22 files" → "23 files").

**Example:** Adding `docs/gotchas/grpc-apm-newprovider.md`:

```markdown
| [[docs/gotchas/grpc-apm-newprovider.md|NewProvider Gotchas]] | NewProvider 3DS callback edge case, mandate webhook idempotency key. |
```

### `profiles/pay-com/` Generated Artifacts

- **New artifact type / directory:** Add it to `## Generated Artifacts` in `profiles/pay-com/AGENTS.md` with Size, Status, and Consumers columns.
- **Storage classification change:** Update the Storage Classification table (e.g. a new `deploy/` dir that is gitignored → add row with **gitignored generated**).

**Example:** Adding `profiles/pay-com/deploy/`:

```markdown
| `deploy/` | **gitignored generated** | Helm charts produced by `scripts/gen_helm.py`. |
```

---

## Health Check Reminder

- **Always** run the health check after AGENTS.md modifications.
- **Fix broken links immediately.** A broken `[...](...)` or `[[...]]` degrades trust in the catalog.
- **Fix count mismatches immediately.** If the tree says `15 modules` but the appendix lists 14, correct the tree.
- If the health check script reports failures, treat them as blocking errors — do not consider the task done until they are resolved.

---

## Backlinks Integrity

The `## Backlinks` section at the bottom of every AGENTS.md is the source of truth for cross-catalog navigation.

### Rules

1. **If adding a new AGENTS.md file** (e.g. for a new profile):
   - Add it to the root AGENTS.md `## Navigable Index` under the correct heading.
   - Add it to the root AGENTS.md `## Backlinks` → "Child catalogs" list.
   - In the new AGENTS.md, add a `## Backlinks` section that points back to the root catalog and to any sibling catalogs.

2. **If removing an AGENTS.md file:**
   - Remove every `[[path/to/that/AGENTS.md|...]]` reference in **all** AGENTS.md files.
   - Remove it from every `## Backlinks` "Child catalogs" or "Linked from" list.
   - Do not leave orphaned wiki-links.

3. **Keep `## Backlinks` synchronized.** When a section anchor changes (e.g. `#gotchas-runtime-traps` → `#gotchas`), update any `[[...#old-anchor|...]]` links.

**Example:** Adding `profiles/acme-corp/AGENTS.md`:

In root `AGENTS.md`:
```markdown
### Profiles
- [[profiles/pay-com/AGENTS.md|Pay-Com Profile Catalog]] ([profiles/pay-com/AGENTS.md](profiles/pay-com/AGENTS.md)) — active production profile
- [[profiles/acme-corp/AGENTS.md|Acme-Corp Profile Catalog]] ([profiles/acme-corp/AGENTS.md](profiles/acme-corp/AGENTS.md)) — new profile
```

In root `AGENTS.md` `## Backlinks`:
```markdown
## Backlinks
- (none inbound — this is the root catalog)
- Child catalogs:
  - [[profiles/pay-com/AGENTS.md|Pay-Com Profile Catalog]]
  - [[profiles/acme-corp/AGENTS.md|Acme-Corp Profile Catalog]]
```

In `profiles/acme-corp/AGENTS.md`:
```markdown
## Backlinks
- Linked from: [[../../AGENTS.md|Root Catalog]] — under "Profiles"
- Sibling catalogs:
  - [[../pay-com/AGENTS.md|Pay-Com Profile Catalog]]
```

---

## Quick Reference Checklist

Before finishing any structural change:

- [ ] Did I read all affected `AGENTS.md` files?
- [ ] Did I update Directory Tree, Storage Classification, and Entry Points as needed?
- [ ] Did I update explicit counts (module counts, file counts, test counts)?
- [ ] Did I add/remove the file in the correct catalog group (Scripts, Tests, Builders, etc.)?
- [ ] Did I keep `[[...]]` for inter-AGENTS and `[...](...)` for code links?
- [ ] Did I run `python scripts/health_check_agents_md.py` and fix all errors?
- [ ] If I added/removed an AGENTS.md file, did I update all `## Backlinks` sections?
- [ ] Did I stage but **not commit** AGENTS.md changes without human review?
