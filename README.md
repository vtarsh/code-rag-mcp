# code-rag-mcp

Local MCP server that indexes an entire GitHub organization into a searchable knowledge base with hybrid search, dependency graph, flow tracing, and task analysis.

**Fully local** — no external APIs, models run on-device. macOS only.

## Quick Start

### Prerequisites

```bash
# Python 3.12+ required
python3 --version

# Install Python packages
pip install mcp lancedb sentence-transformers pydantic PyYAML
# Or if `pip` is not aliased to pip3:
pip3 install mcp lancedb sentence-transformers pydantic PyYAML

# Install system tools
brew install gh jq

# Authenticate GitHub CLI (needed to clone org repos)
gh auth login
```

### Install

```bash
# Clone the repo
git clone https://github.com/vtarsh/code-rag-mcp.git ~/.code-rag
cd ~/.code-rag

# Run the interactive setup wizard
python3 setup_wizard.py
```

The wizard will ask you:
1. **GitHub org name** — the org whose repos you want to index
2. **npm scope** — for detecting internal package dependencies (default: `@<org>`)
3. **Embedding model** — choose between:
   - `coderank` (recommended) — SOTA code embeddings, 768d, ~230MB RAM
   - `minilm` — lightweight general-purpose, 384d, ~80MB RAM, faster to build
4. **Register MCP in Claude Code** — auto-adds to `~/.claude/settings.json`
5. **Install daemon auto-start** — launchd plist for persistent daemon

**Non-interactive mode** (for scripting/LLM):
```bash
python3 setup_wizard.py --org my-github-org --model minilm --no-launchd
```

### Build the index

```bash
# Full build — clones all repos, extracts, indexes, builds vectors + graph
# This takes 30-60 minutes depending on org size
make build

# Or for a specific profile:
make build PROFILE=my-org
```

### Verify it works

```bash
# Run health check
make health

# Run tests
make test
```

## How it works

```
1. clone_repos.sh    — shallow-clone all repos from your GitHub org
2. extract_artifacts  — parse repos into structured artifacts (proto, docs, configs...)
3. build_index        — create SQLite FTS5 full-text search index
4. build_vectors      — create LanceDB vector embeddings for semantic search
5. build_graph        — build dependency graph (gRPC calls, npm deps, proto imports...)
6. daemon.py          — persistent HTTP server holding ML models in memory
7. mcp_server.py      — thin MCP proxy (Claude Code talks to this)
```

## Profiles

Each organization's data is stored in a separate **profile** under `profiles/`:

```
profiles/
├── example/          # Template (shipped in git)
│   ├── config.json
│   ├── glossary.yaml
│   ├── phrase_glossary.yaml
│   ├── known_flows.yaml
│   └── docs/
└── my-org/           # Your org (git-ignored, created by setup wizard)
    ├── config.json
    ├── glossary.yaml         # Domain abbreviations for search expansion
    ├── phrase_glossary.yaml  # Multi-word concept expansion
    ├── known_flows.yaml      # Business flow entry points
    └── docs/
        ├── flows/            # Flow documentation (YAML)
        └── gotchas/          # Gotchas & tips (Markdown)
```

Switch profiles: `make profile PROFILE=my-org`

## Embedding Models

| Model | Key | Dimensions | RAM | Build speed | Best for |
|-------|-----|-----------|-----|-------------|----------|
| CodeRankEmbed | `coderank` | 768 | ~230MB | Slower | Code search (recommended) |
| all-MiniLM-L6-v2 | `minilm` | 384 | ~80MB | Faster | General purpose, quick start |

Switch models: `make switch-model MODEL=minilm`

List models: `python3 scripts/build_vectors.py --list-models`

**Tip**: Start with `minilm` for a quick test, then switch to `coderank` for production quality.

**Disk space**: Embedding models are 80-250 MB (downloaded from HuggingFace on first run). The SQLite database and vector store grow at roughly 1 MB per 10 indexed repos.

## MCP Tools (12)

| Tool | Description |
|------|-------------|
| `search` | Hybrid search (FTS5 + vector + RRF + CrossEncoder). Supports `exclude_file_types` filter |
| `context_builder` | One-call context: search + deps + proto for LLM tasks |
| `repo_overview` | Aggregated info about a specific repo |
| `list_repos` | List/filter repos by type or dependency |
| `find_dependencies` | Bidirectional dependency lookup via graph |
| `trace_impact` | Transitive impact analysis — "if I change X, what breaks?" |
| `trace_flow` | Shortest path between two repos |
| `trace_chain` | Full processing chain from a repo or concept |
| `analyze_task` | Task analysis with proto/webhook/gateway check + GitHub PR scan |
| `diff_provider_config` | Compare feature flags between two providers from seeds.cql |
| `health_check` | System diagnostics |
| `visualize_graph` | Interactive graph visualization |

## Code Facts Extraction

The indexer extracts structured facts from JS/TS code into a `code_facts` table:

| Fact Type | What | Example |
|-----------|------|---------|
| `validation_guard` | if-throw patterns | `if (!mit) → "Only MIT transactions allowed"` |
| `const_value` | UPPER_CASE constants | `MAX_RETRIES = 5` |
| `joi_schema` | Joi validation schemas | `Joi.object({amount: Joi.number()...})` |
| `temporal_retry` | Temporal retry policies | `maximumAttempts: 9, initialInterval: '1h'` |
| `env_var` | process.env with defaults | `process.env.PORT \|\| 3000` |
| `grpc_status` | gRPC status code mapping | `status: NOT_FOUND → "Account not found"` |

Facts are searchable via `search(query, file_type="code_fact")`.

## Makefile Commands

```bash
make help              # Show all commands
make init              # Run setup wizard
make build             # Full pipeline (~30-60 min)
make update            # Incremental update (changed repos only)
make test              # Run tests
make health            # Health check + diagnostics
make switch-model MODEL=minilm  # Rebuild vectors with different model
make register          # Register MCP server in Claude Code
make profile PROFILE=x # Set active profile
make clean             # Remove generated data
```

## Claude Desktop Integration

The setup wizard registers MCP for **Claude Code** automatically. For **Claude Desktop**, add this to your config file manually:

- **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`

```json
{
  "mcpServers": {
    "code-knowledge": {
      "command": "python3",
      "args": ["/path/to/code-rag-mcp/mcp_server.py"],
      "env": {
        "CODE_RAG_HOME": "/path/to/code-rag-mcp",
        "ACTIVE_PROFILE": "my-org"
      }
    }
  }
}
```

Replace `/path/to/code-rag-mcp` with your actual install path (e.g. `/Users/you/.code-rag`) and `my-org` with your profile name. Restart Claude Desktop after saving.

## Architecture

```
mcp_server.py (stdio proxy, ~20MB per session)
      │ HTTP
      ▼
daemon.py (single process, ML models ~230MB, localhost:8742)
      │
      ├── src/search/    FTS5 + vector + RRF + CrossEncoder
      ├── src/graph/     Dependency graph (BFS, pathfinding)
      └── src/tools/     repo_overview, analyze_task, context_builder
```

### Search pipeline

```
Query → glossary expansion → FTS5 (100 candidates)
                           → Vector search (50 candidates)
      → RRF fusion (keyword 2x weight)
      → CrossEncoder reranker (70% rerank + 30% RRF)
      → Top N results
```

## Customizing Your Profile

### Domain glossary (`glossary.yaml`)
Map abbreviations to expanded terms for better search recall:
```yaml
k8s: "kubernetes"
ci: "continuous integration"
api: "application programming interface"
```

### Phrase glossary (`phrase_glossary.yaml`)
Context-aware multi-word expansion:
```yaml
- tokens: [new, service]
  expansion: "boilerplate template scaffolding"
```

### Known flows (`known_flows.yaml`)
Business flow entry points for `trace_chain`:
```yaml
auth:
  - auth-service
  - user-management
```

### Flow documentation (`docs/flows/*.yaml`)
Step-by-step flow annotations for data-driven paths invisible to static analysis.

### Gotchas (`docs/gotchas/*.md`)
Curated institutional knowledge — debugging tips, known quirks, external system behavior.

## Configuration

Profile config (`profiles/<name>/config.json`):
```json
{
  "org": "your-github-org",
  "npm_scope": "@your-org",
  "grpc_domain_suffix": "",
  "server_name": "code-knowledge",
  "display_name": "My Org Knowledge Base",
  "embedding_model": "coderank"
}
```

Environment variables:
- `ACTIVE_PROFILE` — override active profile (default: read from `.active_profile`)
- `CODE_RAG_MODEL` — override embedding model
- `CODE_RAG_HOME` — override base directory (default: `~/.code-rag`)
- `CODE_RAG_PORT` — daemon port (default: 8742)

## Troubleshooting

**`gh auth login` fails**: Make sure you have a GitHub account with access to the org you want to index. You need read access to the repos.

**Build takes too long**: The first build clones all repos. For large orgs (500+ repos), this can take 30-60 min. Subsequent `make update` runs are incremental and much faster.

**Model download is slow**: The first run downloads the embedding model (~100-250MB from HuggingFace). This is a one-time download.

**Daemon won't start**: Check `/tmp/code-rag-daemon.err` for errors. Common issue: port 8742 already in use.

**MCP not showing in Claude Code**: Restart Claude Code after running `make register` or `python3 setup_wizard.py`.

**"0 repos found" during build**: The org name in your profile config is wrong, or `gh` is authenticated to the wrong account. Run `gh auth status` to check which account and org you're logged into.

**Private repos not cloned**: Your GitHub token needs the `repo` scope. Re-run `gh auth login` and make sure the token has full repo access, or use `gh auth refresh -s repo`.

**Small orgs (few repos)**: Works fine. The ANN index is automatically skipped for profiles with fewer than 256 vectors — brute-force search is used instead with no quality loss.

**Verify MCP is working**: Run `make health` to check daemon status, database integrity, vector store, and model loading.

**Manual daemon start (non-launchd)**: If you skipped launchd setup or are on a non-macOS system, start the daemon manually:
```bash
cd ~/.code-rag && python3 daemon.py &
```
The MCP proxy (`mcp_server.py`) will also auto-start the daemon if it is not running.

## Stack

- **Search**: SQLite FTS5 + LanceDB vectors + RRF fusion + CrossEncoder reranker
- **Embeddings**: CodeRankEmbed (768d) or all-MiniLM-L6-v2 (384d)
- **Reranker**: cross-encoder/ms-marco-MiniLM-L-6-v2
- **Graph**: SQLite (nodes + typed edges, hub penalty BFS)
- **MCP**: FastMCP (Python, stdio proxy → HTTP daemon)
- **Platform**: macOS (launchd for auto-start)
