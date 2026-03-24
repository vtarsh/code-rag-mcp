# Deep Research: Structuring Claude Code for Fintech with MCP RAG
# Source: Deep Research, March 2026
# Status: Reference for restructuring — NOT actionable yet, needs audit

## Key Findings

### 1. CLAUDE.md = routing layer (~200 lines max)
- Not a knowledge base
- Point to where docs live, don't embed them
- @import loads eagerly — use sparingly

### 2. Path-scoped rules (we DON'T use yet)
```yaml
---
paths:
  - "src/providers/stripe/**"
---
# Only loads when Claude reads files matching these paths
```
This would reduce context for 27 providers — each provider's rules load ONLY when touching that provider's code.

### 3. Sub-agents CANNOT use MCP tools (confirmed bug #13254)
- Workaround: pre-fetch-and-inject (main agent fetches, passes in prompt)
- Alternative: file-based (write to temp file, sub-agent reads)
- Our agents already use grep/sqlite3 — correct approach

### 4. Progressive disclosure
- Skills: only name+description loaded at startup, full content on invoke
- Rules with paths: loaded on-demand when matching files read
- Subdirectory CLAUDE.md: loaded on-demand

### 5. Local-first enforcement
- CLAUDE.md instructions are advisory (can be ignored)
- PreToolUse hooks are deterministic (guaranteed enforcement)
- Hook on GitHub API calls → check if local git was consulted

### 6. Token budget
- ~150-200 instructions max before adherence degrades
- System prompt uses ~50
- Leaves 100-150 for CLAUDE.md + rules

### 7. Additive inheritance
- All levels load simultaneously, not override
- More specific takes precedence on conflict
- Never duplicate across levels
