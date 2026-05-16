"""Tests for `scripts/benchmark_doc_intent.py` --rerank-on harness (P9).

Three properties we care about (architecture-debate F4):

1. The flag actually wires up a reranker — we don't silently keep bi-encoder
   ordering when the user passed `--rerank-on`.
2. The reranker can change the top-10 ordering. If a model with deterministic
   scores gets fed mismatched candidates, top-10 must reflect rerank score
   order, not the input order.
3. Default behaviour (no flag) preserves bi-encoder semantics — no implicit
   rerank, no model load — so rerun-off bench results stay comparable to the
   pre-P9 historical files.

We test the helper functions directly (no LanceDB / SentenceTransformer fork)
so the suite remains fast and CI-portable.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import scripts.bench.benchmark_doc_intent as bdi

# --- Synthetic reranker -----------------------------------------------------


class FakeReranker:
    """Stand-in for sentence_transformers.CrossEncoder.

    `score_map` maps document text → returned score. Anything not in the map
    falls back to 0.0. We only need the `.predict(pairs)` method because that's
    the surface `rerank_candidates` calls.
    """

    def __init__(self, score_map: dict[str, float]):
        self._score_map = score_map
        self.predict_calls: list[list[tuple[str, str]]] = []

    def predict(self, pairs):
        self.predict_calls.append(list(pairs))
        return [self._score_map.get(doc, 0.0) for _query, doc in pairs]


# --- Tests ------------------------------------------------------------------


def test_rerank_off_default_preserves_behavior():
    """When `reranker=None` the helper returns candidates in the input order.

    This is the no-op path the default --rerank-off bench takes. The function
    must NOT call out to any model and must NOT reshuffle the input.
    """
    candidates = [
        {"repo_name": "r1", "file_path": "a.md", "content_preview": "alpha"},
        {"repo_name": "r1", "file_path": "b.md", "content_preview": "beta"},
        {"repo_name": "r2", "file_path": "c.md", "content_preview": "gamma"},
    ]
    out = bdi.rerank_candidates(reranker=None, query="q", candidates=candidates, limit=10)
    assert [c["file_path"] for c in out] == ["a.md", "b.md", "c.md"]


def test_rerank_on_loads_reranker():
    """`--rerank-on` plumbs a CrossEncoder-shaped object into rerank_candidates.

    We call `rerank_candidates` with a fake reranker and assert that
    `predict()` is invoked exactly once with one (query, doc) pair per
    candidate. This confirms the flag wires real work into the path —
    a regression that bypasses the reranker would skip predict() entirely.
    """
    candidates = [
        {"repo_name": "r1", "file_path": "a.md", "content_preview": "alpha"},
        {"repo_name": "r1", "file_path": "b.md", "content_preview": "beta"},
    ]
    fake = FakeReranker({"r1 a.md alpha": 0.1, "r1 b.md beta": 0.9})
    out = bdi.rerank_candidates(fake, "q", candidates, limit=10)

    assert len(fake.predict_calls) == 1, "predict should fire exactly once per query"
    assert len(fake.predict_calls[0]) == 2, "every candidate should be scored"
    assert all(p[0] == "q" for p in fake.predict_calls[0]), "every pair should carry the query as the first element"
    # Reranker score must surface on the returned dict so callers can trace it.
    assert all("rerank_score" in c for c in out)


def test_rerank_on_changes_top10_order():
    """The reranker reorders bi-encoder top-50 by score desc.

    Candidates are presented in an order that is NOT the rerank order. We
    expect the output to match score-descending order, not input order.
    """
    candidates = [
        {"repo_name": "r", "file_path": "low.md", "content_preview": "low"},
        {"repo_name": "r", "file_path": "high.md", "content_preview": "high"},
        {"repo_name": "r", "file_path": "mid.md", "content_preview": "mid"},
    ]
    fake = FakeReranker(
        {
            "r low.md low": 0.1,
            "r high.md high": 0.9,
            "r mid.md mid": 0.5,
        }
    )
    out = bdi.rerank_candidates(fake, "q", candidates, limit=10)
    paths = [c["file_path"] for c in out]
    assert paths == ["high.md", "mid.md", "low.md"], (
        f"rerank_candidates must sort by reranker score desc, not preserve input order. got {paths}"
    )
    # Score order respects ranking.
    scores = [c["rerank_score"] for c in out]
    assert scores == sorted(scores, reverse=True), f"output should be sorted by rerank_score desc; got {scores}"


def test_rerank_on_respects_limit():
    """`limit=N` caps output to N items even if N candidates were given."""
    candidates = [{"repo_name": "r", "file_path": f"{i}.md", "content_preview": f"c{i}"} for i in range(20)]
    fake = FakeReranker({f"r {i}.md c{i}": float(i) for i in range(20)})
    out = bdi.rerank_candidates(fake, "q", candidates, limit=10)
    assert len(out) == 10
    # Top-10 by score (descending) means file_paths 19..10.
    paths = [c["file_path"] for c in out]
    assert paths == [f"{i}.md" for i in range(19, 9, -1)]


def test_rerank_on_handles_empty_candidates():
    """Zero candidates → empty list, no model call. Mirrors live no-hit query."""
    fake = FakeReranker({})
    out = bdi.rerank_candidates(fake, "q", [], limit=10)
    assert out == []
    assert fake.predict_calls == [], "no candidates ⇒ no predict() call"


def test_e2e_constants_match_production():
    """Sanity: bench harness retrieval-K and rerank model match prod hybrid."""
    assert bdi.E2E_RETRIEVAL_K == 50, (
        "production hybrid_search uses limit=50 in vector_search; "
        "bench retrieval pool must match for apples-to-apples comparison"
    )
    assert bdi.E2E_RERANKER_MODEL == "cross-encoder/ms-marco-MiniLM-L-6-v2", (
        "production reranker is ms-marco-MiniLM-L-6-v2 (src/embedding_provider.py:99); bench must use the same model"
    )


def test_cli_rerank_on_flag_exists():
    """Confirm the --rerank-on flag is registered on the argparse parser.

    We monkey-replace `evaluate_model` and `load_reranker` so the real bench
    pipeline never runs, then call `main()` with --rerank-on plus an empty
    eval set so we can verify the flag was parsed without paying the model
    load cost.
    """
    # Build a tiny eval JSONL so load_eval doesn't sys.exit.
    import json
    import tempfile

    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as tmp:
        # One row with no expected_paths is enough — load_eval just needs valid JSON.
        tmp.write(json.dumps({"id": "t1", "query": "test"}) + "\n")
        tmp_path = tmp.name

    captured: dict = {}

    def fake_evaluate(
        key, eval_rows, common, pre_flight=True, rerank_on=False, reranker=None, stratum_gated=False, dedupe=False
    ):
        captured["rerank_on"] = rerank_on
        captured["reranker_loaded"] = reranker is not None
        captured["stratum_gated"] = stratum_gated
        captured["dedupe"] = dedupe
        return {"model_key": key, "skipped_reason": "test_stub"}

    fake_reranker_loaded: list[bool] = []

    def fake_loader(*args, **kwargs):
        fake_reranker_loaded.append(True)
        return object()  # truthy stand-in

    orig_eval = bdi.evaluate_model
    orig_load = bdi.load_reranker
    orig_argv = sys.argv
    bdi.evaluate_model = fake_evaluate
    bdi.load_reranker = fake_loader

    try:
        sys.argv = [
            "benchmark_doc_intent.py",
            "--model",
            "docs",
            "--eval",
            tmp_path,
            "--no-pre-flight",
            "--rerank-on",
        ]
        rc = bdi.main()
    finally:
        bdi.evaluate_model = orig_eval
        bdi.load_reranker = orig_load
        sys.argv = orig_argv

    assert rc == 0
    assert captured.get("rerank_on") is True, "--rerank-on must propagate to evaluate_model"
    assert captured.get("reranker_loaded") is True, (
        "--rerank-on must trigger load_reranker() before evaluate_model is called"
    )
    assert fake_reranker_loaded == [True], "load_reranker must be called exactly once"


def test_cli_rerank_off_default_skips_load():
    """Default invocation (no --rerank-on) must NOT load the reranker."""
    import json
    import tempfile

    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as tmp:
        tmp.write(json.dumps({"id": "t1", "query": "test"}) + "\n")
        tmp_path = tmp.name

    captured: dict = {}

    def fake_evaluate(
        key, eval_rows, common, pre_flight=True, rerank_on=False, reranker=None, stratum_gated=False, dedupe=False
    ):
        captured["rerank_on"] = rerank_on
        captured["reranker_loaded"] = reranker is not None
        captured["stratum_gated"] = stratum_gated
        captured["dedupe"] = dedupe
        return {"model_key": key, "skipped_reason": "test_stub"}

    fake_reranker_loaded: list[bool] = []

    def fake_loader(*args, **kwargs):
        fake_reranker_loaded.append(True)
        return object()

    orig_eval = bdi.evaluate_model
    orig_load = bdi.load_reranker
    orig_argv = sys.argv
    bdi.evaluate_model = fake_evaluate
    bdi.load_reranker = fake_loader

    try:
        sys.argv = [
            "benchmark_doc_intent.py",
            "--model",
            "docs",
            "--eval",
            tmp_path,
            "--no-pre-flight",
        ]
        rc = bdi.main()
    finally:
        bdi.evaluate_model = orig_eval
        bdi.load_reranker = orig_load
        sys.argv = orig_argv

    assert rc == 0
    assert captured.get("rerank_on") is False, "default should leave rerank_on=False"
    assert captured.get("reranker_loaded") is False, "default path must not pre-load the reranker"
    assert fake_reranker_loaded == [], "load_reranker must NOT be called by default"


# --- Bug 1 fix (2026-04-26): --rerank-model-path CLI override --------------


def _run_main_capture(extra_argv: list[str]) -> dict:
    """Run `bdi.main()` with `evaluate_model`/`load_reranker` stubbed out.

    Returns a dict capturing what the bench would have written to the
    manifest's `rerank_model` field plus the model name passed to
    `load_reranker`. This lets us assert end-to-end CLI -> manifest wiring
    without paying the ML model load cost.
    """
    import json
    import tempfile

    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as tmp:
        tmp.write(json.dumps({"id": "t1", "query": "test"}) + "\n")
        tmp_path = tmp.name

    captured: dict = {}

    def fake_evaluate(
        key,
        eval_rows,
        common,
        pre_flight=True,
        rerank_on=False,
        reranker=None,
        stratum_gated=False,
        dedupe=False,
    ):
        # Read the module-level global at call time exactly like the real
        # `evaluate_model` does when building the manifest at L594. This is
        # the field a downstream consumer would inspect via `jq .rerank_model`.
        captured["manifest_rerank_model"] = bdi.E2E_RERANKER_MODEL if rerank_on else None
        captured["rerank_on"] = rerank_on
        return {"model_key": key, "skipped_reason": "test_stub"}

    def fake_loader(model_name=None, *args, **kwargs):
        # Mirrors `load_reranker(model_name)` signature; record what was
        # passed so we can assert the override flowed through.
        captured["loader_arg"] = model_name
        return object()

    orig_eval = bdi.evaluate_model
    orig_load = bdi.load_reranker
    orig_argv = sys.argv
    bdi.evaluate_model = fake_evaluate
    bdi.load_reranker = fake_loader

    try:
        sys.argv = [
            "benchmark_doc_intent.py",
            "--model",
            "docs",
            "--eval",
            tmp_path,
            "--no-pre-flight",
            "--rerank-on",
            *extra_argv,
        ]
        rc = bdi.main()
    finally:
        bdi.evaluate_model = orig_eval
        bdi.load_reranker = orig_load
        sys.argv = orig_argv

    captured["rc"] = rc
    return captured


def test_rerank_model_path_default_uses_production_model(monkeypatch):
    """Without --rerank-model-path and no env var, manifest should reflect L-6.

    Locks the regression where agents passed `--rerank-model-path=<ft-path>`
    expecting an override; argparse silently dropped the unknown flag and
    the bench reported L-6 results, invalidating the A/B comparison.
    """
    # Reset the module-level global to the production default to simulate
    # a clean import with no env var set.
    monkeypatch.setattr(bdi, "E2E_RERANKER_MODEL", "cross-encoder/ms-marco-MiniLM-L-6-v2")
    out = _run_main_capture(extra_argv=[])

    assert out["rc"] == 0
    assert out["manifest_rerank_model"] == "cross-encoder/ms-marco-MiniLM-L-6-v2"
    assert out["loader_arg"] == "cross-encoder/ms-marco-MiniLM-L-6-v2"


def test_rerank_model_path_flag_overrides_manifest(monkeypatch):
    """--rerank-model-path=/tmp/foo must surface in manifest + loader call.

    Acceptance criterion from `NEXT_SESSION_PROMPT.md` §2 Bug 1: invoking
    with `--rerank-model-path=/tmp/foo` and no env var produces a JSON
    output with `rerank_model: "/tmp/foo"`. Without this lock the bench
    silently uses the default L-6 reranker (the original failure mode).
    """
    monkeypatch.setattr(bdi, "E2E_RERANKER_MODEL", "cross-encoder/ms-marco-MiniLM-L-6-v2")
    out = _run_main_capture(extra_argv=["--rerank-model-path", "/tmp/foo"])

    assert out["rc"] == 0
    assert out["manifest_rerank_model"] == "/tmp/foo", (
        "--rerank-model-path must rebind E2E_RERANKER_MODEL so the manifest "
        "reflects the override (Bug 1 in NEXT_SESSION_PROMPT.md §2)"
    )
    assert out["loader_arg"] == "/tmp/foo", (
        "load_reranker must be called with the override path, not the default "
        "(load_reranker's default kwarg is bound at def time, so we must pass "
        "explicitly after rebinding the global)"
    )


def test_rerank_env_var_still_used_when_flag_absent(monkeypatch):
    """`CODE_RAG_BENCH_RERANKER` env var path must still work without the flag.

    The env var is read at module import time and stored in
    `E2E_RERANKER_MODEL`. We simulate the post-import state by setting the
    global directly (re-importing the module to re-read the env var would
    fight pytest's import caching).
    """
    monkeypatch.setattr(bdi, "E2E_RERANKER_MODEL", "hf:env-only-reranker")
    out = _run_main_capture(extra_argv=[])

    assert out["rc"] == 0
    assert out["manifest_rerank_model"] == "hf:env-only-reranker", (
        "env var path (CODE_RAG_BENCH_RERANKER -> E2E_RERANKER_MODEL) must "
        "still flow into the manifest when --rerank-model-path is absent"
    )
    assert out["loader_arg"] == "hf:env-only-reranker"


def test_rerank_flag_takes_precedence_over_env_var(monkeypatch):
    """When both env var and flag are set, the flag wins.

    The env var was already baked into `E2E_RERANKER_MODEL` at module
    import. The CLI flag rebinds the global afterwards, so the flag value
    is what `evaluate_model` and `load_reranker` see.
    """
    # Simulate "env var was set at import time".
    monkeypatch.setattr(bdi, "E2E_RERANKER_MODEL", "hf:env-only-reranker")
    out = _run_main_capture(extra_argv=["--rerank-model-path", "/workspace/ft-model"])

    assert out["rc"] == 0
    assert out["manifest_rerank_model"] == "/workspace/ft-model", (
        "--rerank-model-path must take precedence over the env var "
        "(precedence is documented in the --rerank-model-path help text)"
    )
    assert out["loader_arg"] == "/workspace/ft-model"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
