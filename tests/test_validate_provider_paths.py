"""Unit tests for scripts/validate_provider_paths.py.

Covers the pure decision logic that drives the verdict:
- spec parsing (OpenAPI 3 + Swagger 2)
- spec discovery (root + swagger/ subfolder)
- host extraction + base-domain detection + façade candidate generation
- placeholder substitution + /api/ stripping
- HTTP status -> semantic classification
- per-spec verdict derivation across the full happy-path / KEEP / NOT_PUBLIC /
  INCONCLUSIVE matrix
- report renderer end-to-end on a fully fabricated input

No network is hit. The script's I/O wrappers (resolve_dns, probe_url) are
exercised only via dependency-injected fakes in the integration smoke test.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

_SPEC = importlib.util.spec_from_file_location(
    "validate_provider_paths", REPO_ROOT / "scripts" / "validate_provider_paths.py"
)
assert _SPEC and _SPEC.loader
vpp = importlib.util.module_from_spec(_SPEC)
# dataclass on Python 3.12 looks up the owning module via sys.modules during
# class creation. Without this registration the import fails with AttributeError.
sys.modules["validate_provider_paths"] = vpp
_SPEC.loader.exec_module(vpp)


# ---------- helpers ---------------------------------------------------------


def _write_spec(path: Path, paths: dict[str, dict], servers: list[str], title: str = "API") -> None:
    payload = {
        "openapi": "3.0.3",
        "info": {"title": title, "version": "v1"},
        "servers": [{"url": u} for u in servers],
        "paths": paths,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _ok_get_path(summary: str = "x") -> dict:
    return {"get": {"summary": summary, "responses": {"200": {"description": "ok"}}}}


# ---------- spec discovery + parsing ----------------------------------------


def test_find_spec_files_prefers_swagger_subdir(tmp_path: Path) -> None:
    sub = tmp_path / "swagger"
    _write_spec(sub / "a.json", {"/x": _ok_get_path()}, ["https://api.example.com"])
    _write_spec(tmp_path / "b.json", {"/y": _ok_get_path()}, ["https://api.example.com"])
    found = vpp.find_spec_files(tmp_path)
    assert {p.name for p in found} == {"a.json", "b.json"}


def test_find_spec_files_filters_non_openapi_json(tmp_path: Path) -> None:
    sub = tmp_path / "swagger"
    _write_spec(sub / "good.json", {"/x": _ok_get_path()}, [])
    (sub / "irrelevant.json").write_text(json.dumps({"foo": "bar"}), encoding="utf-8")
    (sub / "broken.json").write_text("not json{", encoding="utf-8")
    found = vpp.find_spec_files(tmp_path)
    assert [p.name for p in found] == ["good.json"]


def test_parse_spec_openapi3(tmp_path: Path) -> None:
    p = tmp_path / "spec.json"
    _write_spec(
        p,
        {
            "/api/foo": _ok_get_path(),
            "/api/bar/{id}": {
                **_ok_get_path(),
                "post": {"summary": "x", "responses": {"200": {"description": "ok"}}},
            },
        },
        ["https://api.example.com", "https://internal.example.com"],
        title="Demo",
    )
    s = vpp.parse_spec(p)
    assert s is not None
    assert s.title == "Demo"
    assert s.version == "v1"
    assert set(s.spec_paths) == {"/api/foo", "/api/bar/{id}"}
    assert "get" in s.methods_per_path["/api/foo"]
    assert {"get", "post"} == set(s.methods_per_path["/api/bar/{id}"])
    assert s.servers == ["https://api.example.com", "https://internal.example.com"]


def test_parse_spec_swagger2(tmp_path: Path) -> None:
    p = tmp_path / "spec.json"
    p.write_text(
        json.dumps(
            {
                "swagger": "2.0",
                "info": {"title": "Old", "version": "v1"},
                "host": "api.example.com",
                "basePath": "/v2",
                "schemes": ["https"],
                "paths": {"/foo": _ok_get_path()},
            }
        ),
        encoding="utf-8",
    )
    s = vpp.parse_spec(p)
    assert s is not None
    assert s.servers == ["https://api.example.com/v2"]


def test_parse_spec_handles_garbage(tmp_path: Path) -> None:
    p = tmp_path / "broken.json"
    p.write_text("{not json", encoding="utf-8")
    assert vpp.parse_spec(p) is None


# ---------- host extraction + domain detection ------------------------------


def test_extract_hosts_dedupes_and_preserves_order() -> None:
    s1 = vpp.SpecSummary(
        file_name="a",
        title="a",
        version=None,
        spec_paths=[],
        methods_per_path={},
        servers=["https://api.example.com", "https://internal.example.com"],
    )
    s2 = vpp.SpecSummary(
        file_name="b",
        title="b",
        version=None,
        spec_paths=[],
        methods_per_path={},
        servers=["https://api.example.com", "https://test-api.example.com"],
    )
    assert vpp.extract_hosts([s1, s2]) == [
        "api.example.com",
        "internal.example.com",
        "test-api.example.com",
    ]


def test_detect_base_domain_prefers_resolvable() -> None:
    hosts = ["dev-backend.example.com", "backend-v2.internal", "api.example.com"]
    dns = {
        "dev-backend.example.com": vpp.HostDnsStatus("dev-backend.example.com", True),
        "backend-v2.internal": vpp.HostDnsStatus("backend-v2.internal", False, "no DNS"),
        "api.example.com": vpp.HostDnsStatus("api.example.com", True),
    }
    assert vpp.detect_base_domain(hosts, dns) == "example.com"


def test_detect_base_domain_falls_back_when_nothing_resolves() -> None:
    hosts = ["a.example.com", "b.foo.com"]
    dns = {h: vpp.HostDnsStatus(h, False) for h in hosts}
    # Both have 2-label apex, both length-equal — min picks one deterministically.
    assert vpp.detect_base_domain(hosts, dns) in {"example.com", "foo.com"}


def test_detect_base_domain_none_when_no_hosts() -> None:
    assert vpp.detect_base_domain([], {}) is None


def test_detect_base_domain_ignores_localhost_and_internal() -> None:
    """Regression: Inpay's swagger lists localhost + *.internal alongside the
    real *-backend-v2.inpay.com. The presence of localhost as the only
    "resolvable" host must not block apex-domain detection."""
    hosts = [
        "backend-v2.internal",
        "localhost",
        "dev-backend-v2.inpay.com",
        "test-backend-v2.inpay.com",
    ]
    dns = {
        "backend-v2.internal": vpp.HostDnsStatus("backend-v2.internal", False),
        "localhost": vpp.HostDnsStatus("localhost", True),
        "dev-backend-v2.inpay.com": vpp.HostDnsStatus("dev-backend-v2.inpay.com", False),
        "test-backend-v2.inpay.com": vpp.HostDnsStatus("test-backend-v2.inpay.com", False),
    }
    assert vpp.detect_base_domain(hosts, dns) == "inpay.com"


def test_detect_base_domain_skips_ip_literals() -> None:
    hosts = ["127.0.0.1", "10.0.0.5", "api.example.com"]
    dns = {h: vpp.HostDnsStatus(h, True) for h in hosts}
    assert vpp.detect_base_domain(hosts, dns) == "example.com"


def test_generate_facade_candidates_orders_test_first() -> None:
    cands = vpp.generate_facade_candidates("inpay.com")
    assert cands[0] == "https://api.inpay.com"
    # test/sandbox/uat/dev variants must all appear
    expected = {f"https://{e}-api.inpay.com" for e in ("test", "sandbox", "uat", "dev")}
    assert expected.issubset(set(cands))
    # no duplicates
    assert len(cands) == len(set(cands))


# ---------- path utilities ---------------------------------------------------


def test_substitute_placeholders_picks_uuid_for_id() -> None:
    out = vpp.substitute_placeholders("/foo/{order_id}/bar/{uuid}/baz/{anything_else}")
    assert "{order_id}" not in out
    assert out.count("00000000-0000-0000-0000-000000000000") == 2
    assert "/baz/placeholder" in out


def test_substitute_placeholders_handles_reference_and_number() -> None:
    out = vpp.substitute_placeholders("/payment_requests/{inpay_unique_reference}/balance/{account_number}")
    assert "/payment_requests/000000000000/" in out
    assert out.endswith("/balance/1")


def test_strip_api_prefix_only_strips_leading_segment() -> None:
    assert vpp.strip_api_prefix("/api/foo/bar") == "/foo/bar"
    assert vpp.strip_api_prefix("/api") == "/"
    assert vpp.strip_api_prefix("/foo/api/bar") == "/foo/api/bar"
    assert vpp.strip_api_prefix("/customer_app_api/x") == "/customer_app_api/x"


def test_pick_sample_paths_only_gets_and_diversified() -> None:
    spec = vpp.SpecSummary(
        file_name="x",
        title="x",
        version=None,
        spec_paths=["/a/foo", "/a/bar", "/a/baz", "/b/qux"],
        methods_per_path={
            "/a/foo": ["get"],
            "/a/bar": ["get"],
            "/a/baz": ["post"],  # POST excluded
            "/b/qux": ["get"],
        },
        servers=[],
    )
    samples = vpp.pick_sample_paths(spec, max_n=3)
    methods = {m for m, _ in samples}
    assert methods == {"get"}
    paths = [p for _, p in samples]
    # diversification picks /a/foo (first /a/*) and /b/qux, then leftovers
    assert paths[0].startswith("/a/")
    assert "/b/qux" in paths


def test_pick_sample_paths_returns_empty_when_no_gets() -> None:
    spec = vpp.SpecSummary(
        file_name="x",
        title="x",
        version=None,
        spec_paths=["/a/foo"],
        methods_per_path={"/a/foo": ["post"]},
        servers=[],
    )
    assert vpp.pick_sample_paths(spec) == []


# ---------- status semantics -------------------------------------------------


@pytest.mark.parametrize(
    "code,location,content_type,expected",
    [
        # JSON 2xx — clearly a live API
        (200, None, "application/json", vpp.STATUS_LIVE),
        (200, None, "application/octet-stream", vpp.STATUS_LIVE),
        (200, None, None, vpp.STATUS_LIVE),
        (201, None, "application/json", vpp.STATUS_LIVE),
        # 200 with HTML body = edge proxy serving docs/marketing — NOT live
        (200, None, "text/html", vpp.STATUS_REDIRECT_DOCS),
        (200, None, "text/html; charset=utf-8", vpp.STATUS_REDIRECT_DOCS),
        # Auth/validation rejections are live regardless of body type
        (401, None, "text/html", vpp.STATUS_LIVE),
        (403, None, None, vpp.STATUS_LIVE),
        (422, None, "application/json", vpp.STATUS_LIVE),
        (400, None, None, vpp.STATUS_LIVE),
        (404, None, None, vpp.STATUS_NOT_FOUND),
        (302, "https://www.example.com/docs/x", None, vpp.STATUS_REDIRECT_DOCS),
        (302, "https://api.example.com/v2/foo", None, vpp.STATUS_REDIRECT_OTHER),
        (302, None, None, vpp.STATUS_REDIRECT_DOCS),
        (500, None, None, vpp.STATUS_SERVER_ERROR),
        (None, None, None, vpp.STATUS_UNREACHABLE),
    ],
)
def test_classify_status(code, location, content_type, expected) -> None:
    assert vpp.classify_status(code, location, content_type) == expected


# ---------- verdict derivation ----------------------------------------------


def _r(semantic: str):
    return vpp.ProbeResult(url="x", method="GET", status_code=200, location=None, semantic=semantic)


def test_verdict_strip_api_when_originals_dead_strip_live() -> None:
    orig = [_r(vpp.STATUS_REDIRECT_DOCS), _r(vpp.STATUS_REDIRECT_DOCS), _r(vpp.STATUS_NOT_FOUND)]
    strip = [_r(vpp.STATUS_LIVE), _r(vpp.STATUS_LIVE), _r(vpp.STATUS_LIVE)]
    v, _ = vpp.derive_strip_verdict(orig, strip, paths_with_api=3, paths_total=3)
    assert v == vpp.VERDICT_STRIP_API


def test_verdict_keep_api_when_originals_live_strip_dead() -> None:
    orig = [_r(vpp.STATUS_LIVE), _r(vpp.STATUS_LIVE)]
    strip = [_r(vpp.STATUS_NOT_FOUND), _r(vpp.STATUS_NOT_FOUND)]
    v, _ = vpp.derive_strip_verdict(orig, strip, paths_with_api=2, paths_total=2)
    assert v == vpp.VERDICT_KEEP_API


def test_verdict_not_public_when_both_paths_dead() -> None:
    orig = [_r(vpp.STATUS_REDIRECT_DOCS), _r(vpp.STATUS_NOT_FOUND)]
    strip = [_r(vpp.STATUS_REDIRECT_DOCS), _r(vpp.STATUS_NOT_FOUND)]
    v, _ = vpp.derive_strip_verdict(orig, strip, paths_with_api=2, paths_total=2)
    assert v == vpp.VERDICT_NOT_PUBLIC


def test_verdict_inconclusive_when_both_alive() -> None:
    orig = [_r(vpp.STATUS_LIVE)]
    strip = [_r(vpp.STATUS_LIVE)]
    v, _ = vpp.derive_strip_verdict(orig, strip, paths_with_api=1, paths_total=1)
    assert v == vpp.VERDICT_INCONCLUSIVE


def test_verdict_live_as_authored_when_no_api_prefix() -> None:
    orig = [_r(vpp.STATUS_LIVE), _r(vpp.STATUS_LIVE)]
    v, _ = vpp.derive_strip_verdict(orig, [], paths_with_api=0, paths_total=10)
    assert v == vpp.VERDICT_LIVE_AS_AUTHORED


def test_verdict_not_public_when_no_api_prefix_and_all_dead() -> None:
    orig = [_r(vpp.STATUS_REDIRECT_DOCS), _r(vpp.STATUS_NOT_FOUND)]
    v, _ = vpp.derive_strip_verdict(orig, [], paths_with_api=0, paths_total=5)
    assert v == vpp.VERDICT_NOT_PUBLIC


def test_verdict_no_probes_when_empty() -> None:
    v, _ = vpp.derive_strip_verdict([], [], paths_with_api=0, paths_total=0)
    assert v == vpp.VERDICT_NO_PROBES


# ---------- consistency ------------------------------------------------------


def test_consistency_summary_counts_api_prefix() -> None:
    spec = vpp.SpecSummary(
        file_name="x",
        title="x",
        version=None,
        spec_paths=["/api/a", "/api/b", "/c", "/api/d"],
        methods_per_path={},
        servers=[],
    )
    assert vpp.consistency_summary(spec) == (3, 4)


# ---------- report renderer -------------------------------------------------


def test_render_report_contains_all_sections(tmp_path: Path) -> None:
    spec = vpp.SpecSummary(
        file_name="payouts.json",
        title="Payouts",
        version="v1",
        spec_paths=["/api/foo"],
        methods_per_path={"/api/foo": ["get"]},
        servers=["https://api.example.com"],
    )
    outcome = vpp.SpecProbeOutcome(
        spec_name="payouts.json",
        sample_method_paths=[("get", "/api/foo")],
        original_results=[
            vpp.ProbeResult(
                url="https://api.example.com/api/foo",
                method="GET",
                status_code=302,
                location="https://www.example.com/docs/foo",
                semantic=vpp.STATUS_REDIRECT_DOCS,
            )
        ],
        stripped_results=[
            vpp.ProbeResult(
                url="https://api.example.com/foo",
                method="GET",
                status_code=401,
                location=None,
                semantic=vpp.STATUS_LIVE,
            )
        ],
        paths_with_api_prefix=1,
        paths_total=1,
        verdict=vpp.VERDICT_STRIP_API,
        rationale="strip wins",
    )
    dns = {"api.example.com": vpp.HostDnsStatus("api.example.com", True)}
    report = vpp.render_report(
        provider_dir=tmp_path,
        specs=[spec],
        dns_results=dns,
        facade="https://api.example.com",
        facade_log=[
            (
                "https://api.example.com",
                vpp.ProbeResult(
                    url="https://api.example.com/",
                    method="GET",
                    status_code=200,
                    location=None,
                    semantic=vpp.STATUS_LIVE,
                ),
            )
        ],
        outcomes=[outcome],
    )
    assert "DNS resolution" in report
    assert "Public façade discovery" in report
    assert "Per-spec verdicts" in report
    assert "STRIP_API" in report
    assert "/api/foo" in report and "/foo" in report
    assert "Suggested mapping table" in report


def test_render_report_with_no_facade(tmp_path: Path) -> None:
    report = vpp.render_report(
        provider_dir=tmp_path,
        specs=[],
        dns_results={},
        facade=None,
        facade_log=[],
        outcomes=[],
    )
    assert "Public façade: `NONE`" in report
    assert "No public façade reachable" in report
