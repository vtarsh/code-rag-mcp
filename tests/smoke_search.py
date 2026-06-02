"""Search quality smoke suite — fast pytest regression baseline.

Captures today's verified-good search results as expected top-N file paths.
Runs in ~2 minutes (single Python process, loads LanceDB once via fixture).

When a code change unexpectedly bumps a noise file into top-3 / drops a
canonical match below rank 5, the corresponding assertion fails and the
diff is obvious in pytest output.

Designed to be invoked:
  - manually before pushing: `pytest tests/smoke_search.py -v`
  - automatically after the weekly rebuild (see scripts/full_update.sh tail)
  - in CI when wired up (current repo doesn't have CI yet)

The expected ranks come from real `service.search_tool` runs on
2026-05-22 after these env defaults were verified:
  CODE_RAG_QUERY_V2=1
  CODE_RAG_USE_EXPAND_QUERY=1
  CODE_RAG_USE_CAMELCASE_EXPAND=1
  CODE_RAG_DEFAULT_EXCLUDE="package_usage,provider_doc,dictionary"
  CODE_RAG_DEMOTE_TEST_PATHS=1
  CODE_RAG_DEMOTE_TOOLING_REPOS=1

Skipped automatically when:
  - profiles/pay-com/eval/jira_eval_clean_v2.jsonl missing (other profile)
  - db/knowledge.db missing (no index yet)
  - db/vectors.lance.coderank/ missing (no vector tower yet)

NOTE: Some assertions are TOLERANT (top-5 or top-10 containment) because
the reranker is non-deterministic on FP-precision (CPU vs GPU drift,
documented in project_recall_pool_diagnosis_2026_05_19). Tight rank-1
assertions only on queries where rank-1 stability was empirically observed.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def _required_paths_exist() -> bool:
    return (
        (REPO_ROOT / "db" / "knowledge.db").is_file()
        and (REPO_ROOT / "db" / "vectors.lance.coderank").is_dir()
        and (REPO_ROOT / "profiles" / "pay-com").is_dir()
    )


pytestmark = pytest.mark.skipif(
    not _required_paths_exist(),
    reason="db/knowledge.db or db/vectors.lance.coderank/ or profiles/pay-com/ missing — smoke suite requires built indices",
)


@pytest.fixture(scope="module")
def search_env():
    """Set the canonical production env, yield search_tool, restore on teardown."""
    saved = {}
    target_env = {
        "CODE_RAG_HOME": str(REPO_ROOT),
        "ACTIVE_PROFILE": "pay-com",
        "CODE_RAG_QUERY_V2": "1",
        "CODE_RAG_USE_EXPAND_QUERY": "1",
        "CODE_RAG_USE_CAMELCASE_EXPAND": "1",
        "CODE_RAG_DEFAULT_EXCLUDE": "package_usage,provider_doc,dictionary",
        "CODE_RAG_DEMOTE_TEST_PATHS": "1",
        "CODE_RAG_DEMOTE_TOOLING_REPOS": "1",
        # Keep reranker model deterministic across runs
        "CODE_RAG_CODE_RERANKER": "Tarshevskiy/pay-com-rerank-l12-ft-run1",
    }
    for k, v in target_env.items():
        saved[k] = os.environ.get(k)
        os.environ[k] = v

    # Import AFTER env is set — module-level constants read os.environ once.
    from src.search.service import search_tool

    yield search_tool

    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _extract_top_files(result_text: str) -> list[tuple[str, str]]:
    """Parse search_tool output → [(repo_name, file_path), ...] in rank order."""
    files = []
    for line in result_text.splitlines():
        m = re.match(r"\*\*([^*]+)\*\*\s*\|\s*`([^`]+)`", line.strip())
        if m:
            files.append((m.group(1), m.group(2)))
    return files


# -----------------------------------------------------------------------------
# Smoke queries — captured 2026-05-22 with the env above. Each test asserts a
# SPECIFIC expected hit at a TOLERANT rank threshold (top-N) so the assertions
# don't flake on minor rerank drift but DO catch real regressions.
# -----------------------------------------------------------------------------


def _assert_in_top_n(result: str, expected_substring_pairs: list[tuple[str, str]], n: int, label: str):
    """Assert each (repo, file_substring) pair appears in top-N of result."""
    top = _extract_top_files(result)[:n]
    missing = []
    for repo, fpath_substr in expected_substring_pairs:
        if not any(r == repo and fpath_substr in fp for r, fp in top):
            missing.append((repo, fpath_substr))
    assert not missing, f"{label}: missing {missing!r} from top-{n}. Top-{n} actual:\n" + "\n".join(
        f"  {i + 1}. {r}/{p}" for i, (r, p) in enumerate(top)
    )


def _assert_not_in_top_n(result: str, banned_pairs: list[tuple[str, str]], n: int, label: str):
    """Assert none of (repo, file_substring) banned pairs appear in top-N."""
    top = _extract_top_files(result)[:n]
    found = []
    for repo, fpath_substr in banned_pairs:
        for i, (r, fp) in enumerate(top):
            if r == repo and fpath_substr in fp:
                found.append((i + 1, r, fp))
    assert not found, f"{label}: noise found in top-{n}: {found!r}. Demote regressed?"


# -----------------------------------------------------------------------------
# Canonical queries (verified 2026-05-22)
# -----------------------------------------------------------------------------


def test_paypal_disputes_webhook_handler(search_env):
    """Top-3 should contain the actual paypal dispute handlers and webhook routes."""
    r = search_env(query="paypal disputes webhook handler", limit=10, brief=True)
    _assert_in_top_n(
        r,
        [
            ("workflow-provider-webhooks", "paypal/disputes/create-dispute.js"),
            ("workflow-provider-webhooks", "paypal/disputes/update-dispute.js"),
            ("express-webhooks-paypal", "routes/disputes.js"),
        ],
        n=5,
        label="paypal disputes",
    )


def test_webhook_signature_validation(search_env):
    """Top-5 should be webhook signature verification implementations."""
    r = search_env(query="webhook signature validation for payment provider", limit=8, brief=True)
    top = _extract_top_files(r)[:5]
    # All 5 should be from workflow-provider-webhooks
    non_webhook = [(r, p) for r, p in top if r != "workflow-provider-webhooks"]
    assert not non_webhook, f"top-5 should be workflow-provider-webhooks only, got: {non_webhook}"
    # Should mention signature/hash/verify
    signature_terms = ("signature", "verify", "hash", "validate")
    no_sig = [(r, p) for r, p in top if not any(t in p.lower() for t in signature_terms)]
    assert not no_sig, f"top-5 should be signature/verify files, got: {no_sig}"


def test_merchant_onboarding_flow_no_test_noise(search_env):
    """Top-5 should be production code, NOT .spec.js test files (demote fix)."""
    r = search_env(query="how does merchant onboarding flow work", limit=8, brief=True)
    top = _extract_top_files(r)[:5]
    test_files = [(repo, fp) for repo, fp in top if re.search(r"/tests?/|\.spec\.|\.test\.", fp)]
    assert not test_files, (
        f"top-5 leaked test files (CODE_RAG_DEMOTE_TEST_PATHS regression?): {test_files}\nFull top-5: {top}"
    )


def test_add_new_provider_no_ci_noise(search_env):
    """github-run-e2e-action should NOT be in top-5 for product 'how to add' queries."""
    r = search_env(query="how to add a new payment provider integration", limit=10, brief=True)
    _assert_not_in_top_n(
        r,
        [("github-run-e2e-action", "src/index.ts")],
        n=5,
        label="add new provider",
    )
    # Boilerplate template SHOULD be present (template is the answer to "how to add")
    _assert_in_top_n(
        r,
        [("boilerplate-node-providers-grpc-service", "")],
        n=8,
        label="add new provider — boilerplate template",
    )


def test_refund_logic(search_env):
    """Refund processing — top-3 should include refund-named files."""
    r = search_env(query="payment refund processing flow", limit=10, brief=True)
    top = _extract_top_files(r)[:5]
    # Top-5 must have at least 2 refund-named files (verified 2026-05-22 baseline:
    # RefundPaymentModal.tsx @ rank-2, paymentRefund.js @ rank-3)
    refund_hits = sum(1 for _, p in top if "refund" in p.lower())
    assert refund_hits >= 2, f"only {refund_hits}/5 refund-named files in top-5:\n" + "\n".join(
        f"  {i + 1}. {r}/{p}" for i, (r, p) in enumerate(top)
    )


def test_3ds_authentication(search_env):
    """3DS challenge flow — top-10 should hit grpc-mpi-* repos or 3ds-related files."""
    r = search_env(query="3DS challenge authentication flow", limit=10, brief=True)
    top = _extract_top_files(r)[:10]
    three_ds_hits = sum(
        1
        for repo, fp in top
        if "grpc-mpi" in repo or "3ds" in fp.lower() or "three-ds" in fp.lower() or "threeds" in fp.lower()
    )
    assert three_ds_hits >= 3, f"only {three_ds_hits}/10 results 3ds-related:\n" + "\n".join(
        f"  {i + 1}. {r}/{p}" for i, (r, p) in enumerate(top)
    )


def test_settlement_account(search_env):
    """Settlement account queries — top-10 should hit settlement repos / files."""
    r = search_env(query="settlement account configuration logic", limit=10, brief=True)
    top = _extract_top_files(r)[:10]
    settlement_hits = sum(1 for _, p in top if "settlement" in p.lower())
    assert settlement_hits >= 3, f"only {settlement_hits}/10 mention settlement:\n" + "\n".join(
        f"  {i + 1}. {r}/{p}" for i, (r, p) in enumerate(top)
    )


def test_merchant_kyc_compliance(search_env):
    """Compliance / KYC queries — top-10 should hit compliance-related files."""
    r = search_env(query="merchant kyc compliance verification", limit=10, brief=True)
    top = _extract_top_files(r)[:10]
    kyc_hits = sum(
        1
        for _, p in top
        if any(t in p.lower() for t in ("kyc", "compliance", "verification", "merchant-application", "underwriting"))
    )
    assert kyc_hits >= 4, f"only {kyc_hits}/10 results compliance-related:\n" + "\n".join(
        f"  {i + 1}. {r}/{p}" for i, (r, p) in enumerate(top)
    )


def test_chargeback_workflow(search_env):
    """Chargeback workflow — top-10 should hit chargeback-related files."""
    r = search_env(query="chargeback dispute resolution workflow", limit=10, brief=True)
    top = _extract_top_files(r)[:10]
    cb_hits = sum(1 for _, p in top if "chargeback" in p.lower() or "dispute" in p.lower())
    assert cb_hits >= 4, f"only {cb_hits}/10 chargeback/dispute hits:\n" + "\n".join(
        f"  {i + 1}. {r}/{p}" for i, (r, p) in enumerate(top)
    )


def test_no_test_noise_on_generic_how_query(search_env):
    """Sanity: a generic 'how' query should not return test files in top-3 anywhere."""
    queries = [
        "how does payment authorization work",
        "how is provider credential rotation handled",
        "how do we route requests to different acquirers",
    ]
    for q in queries:
        r = search_env(query=q, limit=10, brief=True)
        top = _extract_top_files(r)[:3]
        test_files = [(repo, fp) for repo, fp in top if re.search(r"/tests?/|\.spec\.|\.test\.", fp)]
        assert not test_files, f"query={q!r}: test file in top-3: {test_files}"


def test_pi56_nuvei_handle_activities_via_hard_filter(monkeypatch):
    """Real-world regression: PI-56 'Nuvei expired payment handling' MUST land
    workflow-provider-webhooks/handle-activities.js in top-3 when HARD_FILTER ON.

    Without HARD_FILTER, the reranker confuses payment-expired (PI-56 scope)
    with dispute-expired (workflow-dispute-expiration). With HARD_FILTER, the
    pool excludes non-provider repos entirely.
    """
    # This test sets its own env; isolates from search_env fixture.
    monkeypatch.setenv("CODE_RAG_HOME", str(REPO_ROOT))
    monkeypatch.setenv("ACTIVE_PROFILE", "pay-com")
    monkeypatch.setenv("CODE_RAG_QUERY_V2", "1")
    monkeypatch.setenv("CODE_RAG_USE_EXPAND_QUERY", "1")
    monkeypatch.setenv("CODE_RAG_USE_CAMELCASE_EXPAND", "1")
    monkeypatch.setenv("CODE_RAG_DEFAULT_EXCLUDE", "package_usage,provider_doc,dictionary")
    monkeypatch.setenv("CODE_RAG_DEMOTE_TEST_PATHS", "1")
    monkeypatch.setenv("CODE_RAG_DEMOTE_TOOLING_REPOS", "1")
    monkeypatch.setenv("CODE_RAG_HARD_FILTER", "1")
    monkeypatch.setenv("CODE_RAG_CODE_RERANKER", "Tarshevskiy/pay-com-rerank-l12-ft-run1")

    # Import after env set
    if "src.search.service" in sys.modules:
        # Reload to pick up env-driven module constants if changed
        import importlib

        import src.search.service

        importlib.reload(src.search.service)
    from src.search.service import search_tool

    r = search_tool(query="Enhance Nuvei payment handling for expired transactions", limit=10, brief=True)
    top = _extract_top_files(r)[:3]
    has_pi56 = any(repo == "workflow-provider-webhooks" and "handle-activities" in fp for repo, fp in top)
    assert has_pi56, (
        "PI-56 regression: handle-activities.js should be in top-3 with HARD_FILTER=1.\n"
        + "Top-3 actual:\n"
        + "\n".join(f"  {i + 1}. {r}/{p}" for i, (r, p) in enumerate(top))
    )


def test_no_ci_tooling_on_generic_query(search_env):
    """github-*-action and config-only repos should not surface in top-3 for product queries."""
    queries = [
        "merchant onboarding state machine",
        "payment provider webhook routing",
        "settlement account balance calculation",
    ]
    ci_repo_re = re.compile(r"^github-[\w-]*-action$|-eslint-config$|^lint-|^config-", re.IGNORECASE)
    for q in queries:
        r = search_env(query=q, limit=10, brief=True)
        top = _extract_top_files(r)[:3]
        tool_hits = [(repo, fp) for repo, fp in top if ci_repo_re.search(repo)]
        assert not tool_hits, f"query={q!r}: CI/tooling repo in top-3: {tool_hits}"
