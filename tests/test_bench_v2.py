"""Tests for bench_v2 infrastructure.

Covers:
  * ``sample_bench_v2`` — intent classifier, length bucketing, smoke run.
  * ``bench_v2_gate`` — regression detection, override path, hygiene check.
  * ``benchmark_bench_v2`` — metric primitives and aggregation.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts" / "bench"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sbv2 = _load("sample_bench_v2")
gate = _load("bench_v2_gate")
runner = _load("benchmark_bench_v2")


# ---------------------------------------------------------------------------
# Intent classifier — 6+ edge cases
# ---------------------------------------------------------------------------


def test_intent_doc_by_md_extension():
    assert sbv2.classify_intent("CLAUDE.md rules", set()) == "doc"


def test_intent_doc_by_rules_keyword():
    assert sbv2.classify_intent("rules claude instructions meta", set()) == "doc"


def test_intent_doc_by_readme_token():
    assert sbv2.classify_intent("README overview", set()) == "doc"


def test_intent_doc_by_docquery_re():
    # Hits `_DOC_QUERY_RE` (tutorial/guide/etc) — proposal §2 doc branch.
    assert sbv2.classify_intent("trustly tutorial guide", set()) == "doc"


def test_intent_repo_exact_name():
    repos = {"grpc-apm-trustly", "payper", "grpc-providers-features"}
    assert sbv2.classify_intent("grpc-apm-trustly", repos) == "repo"


def test_intent_repo_must_be_short():
    # Even if repo name appears, >3 tokens disqualifies repo intent.
    repos = {"grpc-apm-trustly"}
    # Hyphenated multi-word stays 1 whitespace-token — still repo.
    assert sbv2.classify_intent("grpc-apm-trustly", repos) == "repo"
    # But adding context tokens pushes past the 3-token cap.
    assert sbv2.classify_intent("grpc-apm-trustly webhook callback status handler", repos) != "repo"


def test_intent_code_camel_case():
    assert sbv2.classify_intent("smartToken createOrder redirect", set()) == "code"


def test_intent_code_snake_case():
    assert sbv2.classify_intent("payment_method_type seeds", set()) == "code"


def test_intent_code_extension_token():
    assert sbv2.classify_intent("initialize.js helpers", set()) == "code"


def test_intent_concept_fallback():
    assert sbv2.classify_intent("how does payout work end to end", set()) == "concept"


def test_intent_doc_beats_code():
    # Priority: doc > repo > code > concept.
    # "docs" keyword + camelCase — should still be doc.
    assert sbv2.classify_intent("docs smartToken explanation", set()) == "doc"


# ---------------------------------------------------------------------------
# Length bucketing — all 4 boundaries
# ---------------------------------------------------------------------------


def test_length_short_boundary():
    assert sbv2.length_bucket("a b c") == "short"  # 3 tokens -> short
    assert sbv2.length_bucket("a b c d") == "medium"  # 4 tokens -> medium


def test_length_medium_boundary():
    assert sbv2.length_bucket("a b c d e f g") == "medium"  # 7 -> medium
    assert sbv2.length_bucket("a b c d e f g h") == "long"  # 8 -> long


def test_length_long_boundary():
    q15 = " ".join(["w"] * 15)
    q16 = " ".join(["w"] * 16)
    assert sbv2.length_bucket(q15) == "long"
    assert sbv2.length_bucket(q16) == "verylong"


def test_length_single_token():
    assert sbv2.length_bucket("trustly") == "short"


# ---------------------------------------------------------------------------
# Provider detector
# ---------------------------------------------------------------------------


def test_provider_word_boundary_avoids_false_match():
    # "wisely" should NOT match "wise" (word boundary).
    assert sbv2.detect_provider("wisely integration flow") is None
    assert sbv2.detect_provider("wise payout flow") == "wise"


def test_provider_detected_case_insensitive():
    assert sbv2.detect_provider("NUVEI card charge") == "nuvei"


# ---------------------------------------------------------------------------
# Sampler smoke test — end-to-end on a tiny synthetic input
# ---------------------------------------------------------------------------


def _write_sampled_jsonl(tmp_path: Path, queries: list[str]) -> Path:
    p = tmp_path / "sampled.jsonl"
    with p.open("w", encoding="utf-8") as f:
        for i, q in enumerate(queries):
            f.write(json.dumps({"query": q, "sampled_ts": f"2026-04-22T10:{i:02d}:00"}) + "\n")
    return p


def test_sampler_smoke_no_db(tmp_path: Path):
    # Mix of intents & lengths — enough pool diversity that all strata can progress.
    queries = [
        "trustly payout flow",  # short / code-ish? camelCase absent -> concept
        "payper initialize smart_token",  # snake_case -> code
        "CLAUDE rules overview",  # doc
        "nuvei verification retry",  # short / concept
        "interac redirect_url webhook handler status",  # code (snake_case)
        "worldpay payout MAX_RETRIES constant",  # code (snake_case? no. SCREAMING but classifier treats as concept)
        "paynearme initialize.js setup create_order redirect",  # code (ext + snake)
        "rules instructions",  # doc / short
        "architecture overview payments end to end",  # concept
        "cashapp smartToken helper",  # code (camel)
    ] * 5  # 50 rows
    inp = _write_sampled_jsonl(tmp_path, queries)
    out = tmp_path / "bench_v2.yaml"
    # db path that doesn't exist — repo intent skipped.
    rc = sbv2.main(
        [
            "--input",
            str(inp),
            "--out",
            str(out),
            "--db",
            str(tmp_path / "nope.db"),
            "-n",
            "20",
            "--seed",
            "42",
        ]
    )
    assert rc == 0
    import yaml

    data = yaml.safe_load(out.read_text())
    assert data["version"] == 2
    assert len(data["queries"]) == 20
    # Every row has required keys and unlabeled fields left null/empty.
    for row in data["queries"]:
        assert set(row.keys()) >= {
            "id",
            "query",
            "answerable",
            "intent",
            "length_bucket",
            "provider",
            "gt_files",
            "gt_symbols",
            "notes",
        }
        assert row["answerable"] is None
        assert row["gt_files"] == []
        assert row["gt_symbols"] == []
        assert row["intent"] in {"code", "concept", "doc", "repo"}
        assert row["length_bucket"] in {"short", "medium", "long", "verylong"}


def test_sampler_determinism(tmp_path: Path):
    queries = [f"query number {i} webhook" for i in range(60)]
    inp = _write_sampled_jsonl(tmp_path, queries)

    out1 = tmp_path / "a.yaml"
    out2 = tmp_path / "b.yaml"
    sbv2.main(["--input", str(inp), "--out", str(out1), "--db", str(tmp_path / "no.db"), "-n", "10", "--seed", "42"])
    sbv2.main(["--input", str(inp), "--out", str(out2), "--db", str(tmp_path / "no.db"), "-n", "10", "--seed", "42"])
    assert out1.read_text() == out2.read_text()


# ---------------------------------------------------------------------------
# Gate logic
# ---------------------------------------------------------------------------


def _make_result(overall: float, strata: dict, unhit: int = 0) -> dict:
    return {
        "overall": {"file_recall@10": overall, "count": 100},
        "strata": {k: {"file_recall@10": v, "count": 10} for k, v in strata.items()},
        "hygiene": {"unanswerable_hits": unhit},
    }


def test_gate_pass_on_improvement(tmp_path: Path):
    baseline = _make_result(0.50, {"intent:code": 0.50, "intent:doc": 0.40})
    current = _make_result(0.60, {"intent:code": 0.60, "intent:doc": 0.45})
    bp = tmp_path / "baseline.json"
    cp = tmp_path / "current.json"
    bp.write_text(json.dumps(baseline))
    cp.write_text(json.dumps(current))
    rc = gate.main(["--current", str(cp), "--baseline", str(bp)])
    assert rc == 0


def test_gate_fail_on_stratum_regression(tmp_path: Path):
    baseline = _make_result(0.50, {"intent:code": 0.50, "intent:doc": 0.40})
    # overall stable, but doc drops -0.05 (> 0.02 threshold).
    current = _make_result(0.50, {"intent:code": 0.50, "intent:doc": 0.35})
    bp = tmp_path / "baseline.json"
    cp = tmp_path / "current.json"
    bp.write_text(json.dumps(baseline))
    cp.write_text(json.dumps(current))
    rc = gate.main(["--current", str(cp), "--baseline", str(bp)])
    assert rc == 1


def test_gate_accept_regression_override(tmp_path: Path):
    baseline = _make_result(0.50, {"intent:code": 0.50, "intent:doc": 0.40})
    current = _make_result(0.50, {"intent:code": 0.50, "intent:doc": 0.35})
    bp = tmp_path / "baseline.json"
    cp = tmp_path / "current.json"
    tracker = tmp_path / "TRACKER.md"
    bp.write_text(json.dumps(baseline))
    cp.write_text(json.dumps(current))
    rc = gate.main(
        [
            "--current",
            str(cp),
            "--baseline",
            str(bp),
            "--tracker",
            str(tracker),
            "--accept-regression",
            "--reason=explicit promotion — P3 tracked in ROADMAP",
        ]
    )
    assert rc == 0
    body = tracker.read_text()
    assert "bench_v2 accepted regression" in body
    assert "intent:doc" in body


def test_gate_hygiene_cannot_be_overridden(tmp_path: Path):
    baseline = _make_result(0.50, {"intent:code": 0.50})
    current = _make_result(0.50, {"intent:code": 0.50}, unhit=2)
    bp = tmp_path / "baseline.json"
    cp = tmp_path / "current.json"
    bp.write_text(json.dumps(baseline))
    cp.write_text(json.dumps(current))
    # Even with override flag, hygiene should block.
    rc = gate.main(
        [
            "--current",
            str(cp),
            "--baseline",
            str(bp),
            "--accept-regression",
            "--reason=try",
        ]
    )
    assert rc == 1


def test_gate_below_threshold_delta_still_passes(tmp_path: Path):
    # Δ = -0.01 is above the -0.02 floor → should pass.
    baseline = _make_result(0.50, {"intent:code": 0.50})
    current = _make_result(0.50, {"intent:code": 0.49})
    bp = tmp_path / "baseline.json"
    cp = tmp_path / "current.json"
    bp.write_text(json.dumps(baseline))
    cp.write_text(json.dumps(current))
    assert gate.main(["--current", str(cp), "--baseline", str(bp)]) == 0


def test_gate_missing_baseline_only_enforces_hygiene(tmp_path: Path):
    current = _make_result(0.50, {"intent:code": 0.50})
    cp = tmp_path / "current.json"
    cp.write_text(json.dumps(current))
    # baseline path does not exist
    rc = gate.main(["--current", str(cp), "--baseline", str(tmp_path / "nope.json")])
    assert rc == 0


# ---------------------------------------------------------------------------
# Runner metric primitives
# ---------------------------------------------------------------------------


def _mk(repo: str, path: str, snippet: str = "") -> dict:
    return {"repo_name": repo, "file_path": path, "snippet": snippet}


def test_file_recall_at_10_intersection():
    gt = ["grpc-apm-trustly::src/methods/initialize.js", "providers-proto::service.proto"]
    results = [
        _mk("grpc-apm-trustly", "src/methods/initialize.js"),
        _mk("grpc-payment-gateway", "src/handler.js"),
        _mk("providers-proto", "service.proto"),
    ]
    assert runner.file_recall_at_10(gt, results) == 1.0


def test_file_recall_at_10_partial():
    gt = ["a::x", "b::y"]
    results = [_mk("a", "x")]
    assert runner.file_recall_at_10(gt, results) == 0.5


def test_file_hit_at_5():
    gt = ["a::x"]
    results = [_mk("a", "x")] + [_mk("z", f"{i}") for i in range(10)]
    assert runner.file_hit_at_5(gt, results) == 1
    # Not in top-5:
    results2 = [_mk("z", f"{i}") for i in range(6)] + [_mk("a", "x")]
    assert runner.file_hit_at_5(gt, results2) == 0


def test_file_mrr_reciprocal():
    gt = ["a::x"]
    # rank-3 hit -> mrr = 1/3
    results = [_mk("z", "1"), _mk("z", "2"), _mk("a", "x")]
    assert abs(runner.file_mrr(gt, results) - 1 / 3) < 1e-9


def test_keyword_recall_counts_snippet_hits():
    results = [
        _mk("r", "f", "this has smart_token call and create_order"),
        _mk("r", "f2", "other content here"),
    ]
    assert runner.keyword_recall(["smart_token", "missing_symbol"], results) == 0.5


def test_aggregate_skips_empty_gt():
    per_query = [
        {
            "id": "BV2-1",
            "file_recall@10": 0.5,
            "file_hit@5": 1,
            "file_mrr": 0.5,
            "keyword_recall": 1.0,
            "counts": True,
            "strata": ["intent:code", "length:short", "provider:none"],
        },
        {
            "id": "BV2-2",
            "file_recall@10": 0.0,
            "file_hit@5": 0,
            "file_mrr": 0.0,
            "keyword_recall": 1.0,
            "counts": False,  # no gt_files
            "strata": ["intent:doc", "length:medium", "provider:none"],
        },
    ]
    rep = runner.aggregate(per_query)
    # Only the counts=True row contributes to overall mean.
    assert rep["overall"]["file_recall@10"] == 0.5
    # intent:code stratum populated, intent:doc excluded (no counts=True rows).
    assert "intent:code" in rep["strata"]
    assert "intent:doc" not in rep["strata"]
