Deep research task. Be concrete and cite 2025-2026 sources. We have already
done the basic diagnostics below — do not just repeat them; build on them.

# Question
Should a 552-repo code-search RAG system be re-architected for the agentic era,
and if so, how? Its only consumer is a capable coding agent (Claude). The
industry is shifting from vector-RAG to agentic grep/glob/read search.

# The system ("code-rag-mcp")
An MCP server indexing 552 internal repos (~80k files) of a payments company,
consumed exclusively by Claude coding agents. Pipeline:
- FTS5 keyword search (SQLite full-text) — grep-like
- LanceDB vector search (CodeRankEmbed embeddings, ~80k chunks)
- RRF fusion of FTS + vector
- CrossEncoder reranker (fine-tuned ms-marco-MiniLM-L-12)
- Dependency graph, ~12.5k cross-repo edges
- MCP tools: search, analyze_task (task→repos/files), trace_chain / trace_flow /
  trace_impact (dependency tracing), repo_overview
Local-first, no external LLM APIs, 16GB Mac + optional RunPod GPU.

# What we measured this week (665 JIRA tasks; ground truth = files changed in
# the linked PRs)
- single-shot file-recall@10 ≈ 0.19; foothold-recall (>=1 file per relevant
  repo in top-10) ≈ 0.63
- recall@pool (top-200) ≈ 0.48 — hard retrieval ceiling; ~60% of expected files
  never even reach the candidate pool
- the wins came from query-processing fixes (stopword removal, OR-term dedup,
  doc-noise demotion); reranker fine-tuning has 1 success in a long failure history
- iterative query reformulation helps sometimes, not mostly; if retrieval
  surfaces ZERO files from the right repo the agent has no thread to follow
- recall@10 is capped at ~0.77 by task size alone (many tasks touch 20-180 files)

# Research questions
1. 2026 state of the art for code retrieval serving coding agents. How do
   leading code agents actually retrieve — Claude Code, Cursor, Cognition/Devin,
   Augment, Sourcegraph/Amp, Windsurf: grep, vector, hybrid, graph, LSP?
2. When does vector embedding retrieval still earn its cost over
   keyword + agentic iteration for CODE specifically? State the conditions.
3. Is a cross-repo dependency graph a durable advantage for "what breaks if I
   change X" reasoning that grep/agentic search cannot replace? Evidence.
4. What is the right success metric for a retrieval tool whose consumer is an
   iterating agent, as opposed to single-shot recall@k?
5. Concrete recommendation for THIS system — what to keep / simplify / cut.
   Evaluate at least: (a) keep hybrid as-is; (b) drop vector+reranker, keep
   FTS + graph + agentic iteration; (c) full pivot to a grep-first agent
   toolkit (ripgrep + LSP + the graph); (d) anything better.

# Deliverable
A decision with reasoning, a recommended target architecture, and a phased
migration plan (what to do first, what to measure, what to delete and when).
Concrete over abstract.
