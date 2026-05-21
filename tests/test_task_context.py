"""Unit tests for src/tools/task_context.py — Step 2 helper."""

from src.tools.task_context import build_body_query, extract_code_anchored, sanitize_body

# --- sanitize_body --------------------------------------------------------


def test_sanitize_strips_urls():
    s = sanitize_body("see https://jira.foo/issue/X and http://bar.com/y stuff")
    assert "https://" not in s
    assert "http://" not in s
    assert "stuff" in s


def test_sanitize_strips_jwt():
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abc-xyz_0123"
    s = sanitize_body(f"token: {jwt} after")
    assert jwt not in s
    assert "after" in s


def test_sanitize_strips_creds():
    s = sanitize_body("api_key: deadbeef123 password = hunter2 x-api-key: ZZZ")
    assert "deadbeef123" not in s
    assert "hunter2" not in s
    assert "ZZZ" not in s


def test_sanitize_strips_hex_hash():
    s = sanitize_body("commit abc123def456789012345678901234567890 stuff")
    assert "abc123def456789012345678901234567890" not in s
    assert "stuff" in s


def test_sanitize_strips_fts5_breakers():
    s = sanitize_body("Error: Code: 60. DB::Exception, broken (parens) [tags]")
    for ch in (":", ",", "[", "]", "(", ")"):
        assert ch not in s, f"{ch!r} should be stripped"


def test_sanitize_md_inline_code_preserved():
    s = sanitize_body("call `useMerchantPricing()` from `MerchantPage.tsx`")
    assert "`" not in s
    assert "useMerchantPricing" in s


def test_sanitize_md_fences_unwrapped():
    body = "describe ```python\nfoo_bar(x)\n``` end"
    s = sanitize_body(body)
    assert "```" not in s
    assert "foo_bar" in s


def test_sanitize_handles_empty():
    assert sanitize_body("") == ""
    assert sanitize_body(None) == ""  # type: ignore[arg-type]


# --- extract_code_anchored ------------------------------------------------


def test_extract_pascal_camel_snake():
    text = "use SettlementAccount and getMerchantBalance via get_routing_details"
    out = extract_code_anchored(text)
    assert "SettlementAccount" in out
    assert "getMerchantBalance" in out
    assert "get_routing_details" in out


def test_extract_hyphenated():
    text = "the request-logs flow and api-keys table"
    out = extract_code_anchored(text)
    assert "request-logs" in out
    assert "api-keys" in out


def test_extract_filepath():
    text = "see MerchantPage.tsx and schemas.proto"
    out = extract_code_anchored(text)
    assert "MerchantPage.tsx" in out
    assert "schemas.proto" in out


def test_extract_abbrev_skips_stopwords():
    text = "use the HTTP API for JSON over JWT, with CDC backed by SQL"
    out = extract_code_anchored(text)
    assert "HTTP" not in out
    assert "API" not in out
    assert "JSON" not in out
    assert "JWT" in out or "CDC" in out


def test_extract_orders_filepath_above_plain():
    text = "MerchantPage.tsx and MerchantPage.tsx and SimpleName"
    out = extract_code_anchored(text)
    assert out[0] == "MerchantPage.tsx"


def test_extract_min_length():
    text = "use abc Short ifok"
    out = extract_code_anchored(text)
    assert all(len(t) >= 3 for t in out)


def test_extract_empty():
    assert extract_code_anchored("") == []
    assert extract_code_anchored(None) == []  # type: ignore[arg-type]


# --- build_body_query -----------------------------------------------------


def test_build_body_query_returns_none_on_empty():
    assert build_body_query("", "any") is None
    assert build_body_query(None, "any") is None  # type: ignore[arg-type]


def test_build_body_query_returns_none_on_pure_prose():
    body = "We would like to refactor the flow to reduce time. So the new approach is better."
    assert build_body_query(body, "Some title") is None


def test_build_body_query_drops_overlap_with_title():
    body = "We use MerchantPage.tsx in the SettlementAccount flow"
    title = "MerchantPage Settlement"
    q = build_body_query(body, title)
    if q is not None:
        for tok in q.split():
            assert tok.lower() not in title.lower()


def test_build_body_query_keeps_code_signal():
    body = (
        "Refactor API logs flow to use ScyllaDB Table and CDC logs. "
        "Create grpc-actions-producer with request-logs in workflowId."
    )
    title = "Refactor api logs"
    q = build_body_query(body, title)
    assert q is not None
    assert any(tok in q for tok in ("grpc-actions-producer", "request-logs", "workflowId"))


def test_sanitize_is_idempotent():
    body = "see https://x.foo `foo_bar` MerchantPage.tsx"
    s1 = sanitize_body(body)
    s2 = sanitize_body(s1)
    assert s1 == s2
