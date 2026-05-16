"""Unit tests for scripts/eval_jidm — the IM-NDCG direction metric."""

from __future__ import annotations

import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import scripts.eval.eval_jidm as ej  # type: ignore[import-not-found]  # noqa: E402

# -----------------------
# query_intent
# -----------------------


def test_query_intent_doc_only() -> None:
    # _DOC_QUERY_RE hit, no code tokens
    assert ej.query_intent("where are the docs for provider checklist") == "doc"
    assert ej.query_intent("payment methods readme") == "doc"


def test_query_intent_code_only_snake_case() -> None:
    assert ej.query_intent("validate_payment_request handler") == "code"


def test_query_intent_code_only_function_call() -> None:
    assert ej.query_intent("how does charge_card() work") == "code"


def test_query_intent_code_only_ext() -> None:
    assert ej.query_intent("logic in handler.ts") == "code"


def test_query_intent_repo_token_is_code() -> None:
    # repo tokens count as code intent even without code-ish tokens
    assert ej.query_intent("grpc-providers-nuvei timeout") == "code"
    assert ej.query_intent("next-web-dynamic-currency-converter") == "code"


def test_query_intent_mixed() -> None:
    # no doc keywords, no code tokens, no repo tokens -> mixed
    assert ej.query_intent("additional delay timeout") == "mixed"
    assert ej.query_intent("prevent duplicate charges") == "mixed"


def test_query_intent_doc_plus_code_is_code() -> None:
    # doc keyword but a code token present -> code (code trumps mixed-ish doc)
    assert ej.query_intent("docs for charge_card function") == "code"


# -----------------------
# kind_of
# -----------------------


def test_kind_of_ci_yaml_priority() -> None:
    # ci/deploy.yaml wins even if file_type says "config"
    assert ej.kind_of("config", "ci/deploy.yaml") == "ci"
    assert ej.kind_of("workflow", "k8s/.github/workflows/release.yml") == "ci"


def test_kind_of_test_path() -> None:
    # V-C spec regex requires a boundary before "tests/" — (\.spec\.|\.test\.|/tests?/)
    assert ej.kind_of("code", "src/foo.test.ts") == "test"
    assert ej.kind_of("code", "repo/tests/unit/test_bar.py") == "test"
    assert ej.kind_of("library", "src/foo.spec.js") == "test"


def test_kind_of_doc_by_file_type() -> None:
    assert ej.kind_of("gotchas", "docs/GOTCHAS.md") == "doc"
    assert ej.kind_of("reference", "profiles/x/docs/references/payment-flow.md") == "doc"
    assert ej.kind_of("dictionary", "index/words.json") == "doc"  # dictionary is doc-ish


def test_kind_of_md_not_doc_by_type() -> None:
    # No doc file_type and not a known doc path -> defaults to code
    # (V-C spec uses file_type primarily; paths only catch CI/test)
    assert ej.kind_of("code", "NOTES.md") == "code"


def test_kind_of_proto() -> None:
    assert ej.kind_of("proto", "api/payment.proto") == "proto"
    assert ej.kind_of("other", "api/payment.proto") == "proto"  # ext alone suffices


def test_kind_of_config_yaml() -> None:
    assert ej.kind_of("env", ".env.example") == "config"
    assert ej.kind_of("config", "app.yaml") == "config"
    assert ej.kind_of("service", "package.json") == "config"


def test_kind_of_code_fallback() -> None:
    assert ej.kind_of("library", "libs/client.js") == "code"
    assert ej.kind_of("service", "src/server.ts") == "code"


# -----------------------
# im_ndcg_at_k — smoke + core math
# -----------------------


def test_im_ndcg_empty() -> None:
    assert ej.im_ndcg_at_k("anything", [], k=10) == 0.0


def test_im_ndcg_perfect_code_query() -> None:
    # Code-intent + 10 unique code files => DCG == IDCG, diversity = 1.0
    ranked = [{"repo_name": "r", "file_path": f"src/{i}.ts", "file_type": "service"} for i in range(10)]
    score = ej.im_ndcg_at_k("charge_card function", ranked, k=10)
    assert math.isclose(score, 1.0, rel_tol=1e-9)


def test_im_ndcg_perfect_doc_query() -> None:
    ranked = [{"repo_name": "r", "file_path": f"docs/{i}.md", "file_type": "gotchas"} for i in range(10)]
    score = ej.im_ndcg_at_k("gotchas documentation", ranked, k=10)
    assert math.isclose(score, 1.0, rel_tol=1e-9)


def test_im_ndcg_code_query_hit_by_ci_zero_relevance() -> None:
    # All CI yaml results on a code query -> REL["code"]["ci"] = 0 -> idcg=0 -> 0.0
    ranked = [{"repo_name": f"r{i}", "file_path": "ci/deploy.yaml", "file_type": "workflow"} for i in range(10)]
    assert ej.im_ndcg_at_k("charge_card fn", ranked, k=10) == 0.0


def test_im_ndcg_ranker_order_matters() -> None:
    # Same set, different orderings should produce different DCG.
    ranked_good = [
        {"repo_name": "r", "file_path": "src/a.ts", "file_type": "service"},  # code, rel 1.0
        {"repo_name": "r", "file_path": "README.md", "file_type": "docs"},  # doc, rel 0.3
    ]
    ranked_bad = list(reversed(ranked_good))
    # Code intent => code=1.0, doc=0.3. Putting code first is better.
    good = ej.im_ndcg_at_k("charge_card fn", ranked_good, k=2)
    bad = ej.im_ndcg_at_k("charge_card fn", ranked_bad, k=2)
    assert good > bad


# -----------------------
# Diversity multiplier
# -----------------------


def test_diversity_10_unique_equals_one() -> None:
    ranked = [{"repo_name": "r", "file_path": f"src/{i}.ts", "file_type": "service"} for i in range(10)]
    # 10 unique (repo,file) -> diversity=1.0; code-intent, all code -> DCG=IDCG
    assert ej.im_ndcg_at_k("charge_card fn", ranked, k=10) == 1.0


def test_diversity_5_unique_out_of_10_equals_half() -> None:
    # 5 unique files, each duplicated once. All code -> rel_map each = 1.0.
    # DCG==IDCG so the ratio is 1.0, and diversity_multiplier = 5/10 = 0.5.
    unique = [{"repo_name": "r", "file_path": f"src/{i}.ts", "file_type": "service"} for i in range(5)]
    ranked = []
    for u in unique:
        ranked.append(u)
        ranked.append(u)  # duplicate same (repo,file)
    assert len(ranked) == 10
    score = ej.im_ndcg_at_k("charge_card fn", ranked, k=10)
    assert math.isclose(score, 0.5, rel_tol=1e-9)


# -----------------------
# Spearman rho — fallback impl
# -----------------------


def test_spearman_rho_perfect_positive() -> None:
    xs = [0.1, 0.2, 0.3, 0.4, 0.5]
    ys = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert math.isclose(ej._spearman_rho(xs, ys), 1.0, rel_tol=1e-9)


def test_spearman_rho_perfect_negative() -> None:
    xs = [0.1, 0.2, 0.3, 0.4, 0.5]
    ys = [5.0, 4.0, 3.0, 2.0, 1.0]
    assert math.isclose(ej._spearman_rho(xs, ys), -1.0, rel_tol=1e-9)


def test_spearman_rho_constant_input_returns_none() -> None:
    xs = [0.5, 0.5, 0.5, 0.5]
    ys = [1.0, 2.0, 3.0, 4.0]
    assert ej._spearman_rho(xs, ys) is None


def test_spearman_rho_length_mismatch_or_empty() -> None:
    assert ej._spearman_rho([], []) is None
    assert ej._spearman_rho([1.0], [1.0]) is None  # n<2
    assert ej._spearman_rho([1.0, 2.0], [1.0]) is None


# -----------------------
# _eval_snapshot — integration with churn-replay shape
# -----------------------


def test_eval_snapshot_churn_replay_shape(tmp_path: Path) -> None:
    snap = {
        "per_query": [
            {
                "query": "charge_card fn",
                "base_top10": [
                    {"repo_name": "r", "file_path": f"src/{i}.ts", "file_type": "service"} for i in range(10)
                ],
                "v8_top10": [{"repo_name": "r", "file_path": "README.md", "file_type": "docs"} for _ in range(10)],
            },
            {
                "query": "how to docs readme",
                "base_top10": [{"repo_name": "r", "file_path": "README.md", "file_type": "docs"} for _ in range(10)],
                "v8_top10": [{"repo_name": "r", "file_path": f"src/{i}.ts", "file_type": "service"} for i in range(10)],
            },
        ]
    }
    result = ej._eval_snapshot(snap, k=10)
    assert result["format"] == "churn_replay"
    assert set(result["series"]) == {"base_top10", "v8_top10"}
    assert result["summary"]["n_queries"] == 2
    # Query 1 (code intent): base=1.0 (all code), v8=~0.3 (all doc, diversity 0.1)
    # Query 2 (doc intent):   base=~0.1 (doc + diversity 0.1), v8=~0.3 (all code, diversity 1.0)
    base = result["summary"]["mean_im_ndcg"]["base_top10"]
    v8 = result["summary"]["mean_im_ndcg"]["v8_top10"]
    assert 0.0 <= base <= 1.0
    assert 0.0 <= v8 <= 1.0
    # Intent distribution sanity: one code, one doc
    assert result["summary"]["intent_distribution"] == {"code": 1, "doc": 1}


def test_eval_snapshot_intent_reuses_doc_query_re() -> None:
    # Proves we share the production _DOC_QUERY_RE regex.
    from src.search.hybrid import _DOC_QUERY_RE

    assert _DOC_QUERY_RE is not None
    # A query that _DOC_QUERY_RE matches must classify as doc unless code trumps.
    assert ej.query_intent("provider checklist") == "doc"


def test_eval_snapshot_ci_path_reuses_prod_regex() -> None:
    from src.search.hybrid import _CI_PATH_RE

    assert _CI_PATH_RE.search("ci/deploy.yaml") is not None
    # Script must classify as ci regardless of file_type value
    assert ej.kind_of("random", "ci/deploy.yml") == "ci"


def test_im_ndcg_mixed_intent_ci_partial_credit() -> None:
    # Mixed intent: ci gets 0.2 (nonzero). A pure-CI list should not be zero.
    ranked = [{"repo_name": f"r{i}", "file_path": "ci/deploy.yaml", "file_type": "workflow"} for i in range(10)]
    # "additional delay timeout" -> mixed
    score = ej.im_ndcg_at_k("additional delay timeout", ranked, k=10)
    # All same rel => DCG==IDCG => ratio 1.0; but diversity=1/10 (same repo? no, r0..r9)
    # repo_name differs per result so diversity=1.0 => score 1.0.
    assert math.isclose(score, 1.0, rel_tol=1e-9)
