"""Tests for trace_field tool — specifically the provider filter fix."""

from src.tools.fields import _filter_hops_by_provider, _hop_provider_tag


def test_hop_provider_tag_grpc_apm():
    assert _hop_provider_tag({"service": "grpc-apm-volt", "file": "libs/map-response.js"}) == "volt"
    assert _hop_provider_tag({"service": "grpc-apm-payper", "file": "libs/map-response.js"}) == "payper"


def test_hop_provider_tag_grpc_providers():
    assert _hop_provider_tag({"service": "grpc-providers-nuvei", "file": "libs/index.js"}) == "nuvei"


def test_hop_provider_tag_shared_providers_repo():
    # grpc-providers-credentials / features / proto are SHARED — not a single provider.
    assert _hop_provider_tag({"service": "grpc-providers-credentials", "file": "seeds.cql"}) == ""
    assert _hop_provider_tag({"service": "grpc-providers-features", "file": "seeds.cql"}) == ""
    # But when the file sits under libs/<provider>/, the hop IS provider-scoped.
    assert _hop_provider_tag({"service": "grpc-providers-credentials", "file": "libs/payper/index.js"}) == "payper"


def test_hop_provider_tag_service_parens():
    hop = {"service": "workflow-provider-webhooks (volt)", "file": "activities/volt/payment/handle-activities.js"}
    assert _hop_provider_tag(hop) == "volt"


def test_hop_provider_tag_activities_path():
    # Falls back to file path when service name is generic.
    hop = {"service": "workflow-provider-webhooks", "file": "activities/paysafe/payment/handle-activities.js"}
    assert _hop_provider_tag(hop) == "paysafe"


def test_hop_provider_tag_generic():
    # Shared services with no provider scope.
    assert _hop_provider_tag({"service": "grpc-payment-gateway", "file": "libs/call-providers-gateway.js"}) == ""
    assert _hop_provider_tag({"service": "node-libs-providers-common", "file": "libs/get-credentials.js"}) == ""


def test_filter_hops_by_provider_keeps_neutral_and_matching():
    hops = [
        {"service": "grpc-apm-volt", "file": "libs/map-response.js"},
        {"service": "grpc-apm-payper", "file": "libs/map-response.js"},
        {"service": "grpc-payment-gateway", "file": "libs/call-providers-gateway.js"},
        {"service": "workflow-provider-webhooks (trustly)", "file": "activities/trustly/x.js"},
    ]
    result = _filter_hops_by_provider(hops, "payper")
    services = [h["service"] for h in result]
    assert "grpc-apm-payper" in services
    assert "grpc-payment-gateway" in services  # provider-neutral
    assert "grpc-apm-volt" not in services
    assert "workflow-provider-webhooks (trustly)" not in services


def test_filter_hops_by_provider_empty_provider_returns_all():
    hops = [
        {"service": "grpc-apm-volt", "file": "libs/map-response.js"},
        {"service": "grpc-payment-gateway", "file": "x.js"},
    ]
    assert _filter_hops_by_provider(hops, "") == hops


def test_filter_hops_preserves_non_dict_entries():
    # Some YAML chains may contain string hops; they should pass through unchanged.
    hops = [
        "some plain text hop",
        {"service": "grpc-apm-volt"},
        {"service": "grpc-apm-payper"},
    ]
    result = _filter_hops_by_provider(hops, "payper")
    assert "some plain text hop" in result
    assert {"service": "grpc-apm-payper"} in result
    assert {"service": "grpc-apm-volt"} not in result


def test_trace_field_tool_filters_by_provider(tmp_path, monkeypatch):
    """Integration: trace_field_tool with provider=payper must not return volt hops."""
    from src.tools import fields as fields_module

    fake_chains = {
        "fields": {
            "processorTransactionId": {
                "hops": [
                    {"service": "grpc-apm-volt", "file": "libs/map-response.js", "role": "producer"},
                    {"service": "grpc-apm-payper", "file": "libs/map-response.js", "role": "producer"},
                    {"service": "grpc-payment-gateway", "file": "methods/sale.js", "role": "consumer"},
                    {"service": "workflow-provider-webhooks (volt)", "file": "activities/volt/x.js", "role": "consumer"},
                ],
            },
        },
    }
    fields_module._cache["trace-chains.yaml"] = fake_chains
    fields_module._cache["field-contracts.yaml"] = {}
    fields_module._cache["reference-snapshots.yaml"] = {}

    out = fields_module.trace_field_tool("processorTransactionId", provider="payper", mode="trace")
    assert "grpc-apm-payper" in out
    assert "grpc-payment-gateway" in out  # neutral, kept
    assert "grpc-apm-volt" not in out
    assert "workflow-provider-webhooks (volt)" not in out
    assert "filtered to `payper`" in out

    # Sanity: without provider, all hops appear.
    out_all = fields_module.trace_field_tool("processorTransactionId", provider="", mode="trace")
    assert "grpc-apm-volt" in out_all
    assert "grpc-apm-payper" in out_all

    # Clean up cache so other tests aren't affected.
    fields_module._cache.clear()
