#!/usr/bin/env python3
"""Build shadow type maps for a provider.

Combines proto schema parsing + JS field extraction into a per-provider YAML
that shows the full type chain from gateway proto to external API and back.

Usage:
    python scripts/build_shadow_types.py --provider=payper
    python scripts/build_shadow_types.py --provider=payper --method=initialize
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import yaml

# Ensure project root on path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
from scripts._common import setup_paths

setup_paths()

from src.js_field_extractor import extract_fields_from_directory
from src.proto_parser import merge_schemas, parse_proto_path
from src.types import FieldMapping, FieldUsage, MethodTypeMap, ProviderTypeMap

# ---------------------------------------------------------------------------
# Provider configs — maps provider name to source locations
# ---------------------------------------------------------------------------

WORK_DIR = Path(os.environ.get("PAY_COM_WORK_DIR", Path.home() / "work" / "pay-com"))


def _apm_config(
    repo: str, api_endpoints: dict[str, tuple[str, str]] | None = None, extra_methods: dict[str, dict] | None = None
) -> dict:
    """Build standard APM provider config. All APM providers share the same proto types."""
    endpoints = api_endpoints or {}
    methods = {
        "initialize": {
            "proto_request": "InitializeRequest",
            "proto_response": "InitializeResponse",
            "api_endpoint": endpoints.get("initialize", ("", "POST"))[0],
            "api_method": endpoints.get("initialize", ("", "POST"))[1],
        },
        "sale": {
            "proto_request": "SaleRequest",
            "proto_response": "SaleResponse",
            "api_endpoint": endpoints.get("sale", ("", "POST"))[0],
            "api_method": endpoints.get("sale", ("", "POST"))[1],
        },
        "refund": {
            "proto_request": "RefundRequest",
            "proto_response": "RefundResponse",
            "api_endpoint": endpoints.get("refund", ("", "POST"))[0],
            "api_method": endpoints.get("refund", ("", "POST"))[1],
        },
    }
    if extra_methods:
        for name, cfg in extra_methods.items():
            methods[name] = {
                "proto_request": cfg.get("proto_request", f"{name.title()}Request"),
                "proto_response": cfg.get("proto_response", f"{name.title()}Response"),
                "api_endpoint": cfg.get("api_endpoint", ""),
                "api_method": cfg.get("api_method", "POST"),
            }
    return {
        "repo_name": repo,
        "proto_files": [
            "providers-proto/service.proto",
            f"{repo}/node_modules/@pay-com/providers-proto/service.proto",
        ],
        "methods_dir": f"{repo}/methods",
        "libs_dir": f"{repo}/libs",
        "service_name": "ProviderService",
        "method_config": methods,
    }


PROVIDER_CONFIGS: dict[str, dict] = {
    "payper": _apm_config(
        "grpc-apm-payper",
        {
            "initialize": ("/payment/eTransfer/", "POST"),
            "sale": ("/status/{processorTransactionId}", "GET"),
            "refund": ("/refund", "POST"),
        },
        extra_methods={
            "payout": {
                "proto_request": "PayoutRequest",
                "proto_response": "PayoutResponse",
                "api_endpoint": "/payout/eTransfer/",
                "api_method": "POST",
            },
        },
    ),
    "trustly": _apm_config(
        "grpc-apm-trustly",
        {
            "initialize": ("/api/1", "POST"),
            "sale": ("/api/1", "POST"),
            "refund": ("/api/1", "POST"),
        },
        extra_methods={
            "verification": {
                "proto_request": "InitializeRequest",
                "proto_response": "InitializeResponse",
                "api_endpoint": "/api/1",
            },
            "completion": {"proto_request": "SaleRequest", "proto_response": "SaleResponse", "api_endpoint": "/api/1"},
            "payout": {"proto_request": "RefundRequest", "proto_response": "RefundResponse", "api_endpoint": "/api/1"},
        },
    ),
    "nuvei": _apm_config(
        "grpc-providers-nuvei",
        {
            "initialize": ("/getSessionToken.do + /payment.do", "POST"),
            "sale": ("/getPaymentStatus.do", "POST"),
            "refund": ("/refundTransaction.do", "POST"),
        },
        extra_methods={
            "authorization": {"proto_request": "SaleRequest", "proto_response": "SaleResponse"},
            "cancellation": {"proto_request": "SaleRequest", "proto_response": "SaleResponse"},
            "completion": {"proto_request": "SaleRequest", "proto_response": "SaleResponse"},
            "verification": {"proto_request": "InitializeRequest", "proto_response": "InitializeResponse"},
            "payout": {"proto_request": "RefundRequest", "proto_response": "RefundResponse"},
        },
    ),
    "fonix": _apm_config(
        "grpc-apm-fonix",
        {
            "initialize": ("/chargesend", "POST"),
            "sale": ("/status", "GET"),
            "refund": ("/refund", "POST"),
        },
        extra_methods={
            "completion": {"proto_request": "SaleRequest", "proto_response": "SaleResponse"},
        },
    ),
    "aircash": _apm_config(
        "grpc-apm-aircash",
        {
            "initialize": ("/frame", "POST"),
            "sale": ("/status", "GET"),
            "refund": ("/refund", "POST"),
        },
        extra_methods={
            "payout": {"proto_request": "RefundRequest", "proto_response": "RefundResponse"},
        },
    ),
    "neosurf": _apm_config(
        "grpc-apm-neosurf",
        {
            "initialize": ("/initialize", "POST"),
            "sale": ("/status", "GET"),
            "refund": ("/refund", "POST"),
        },
    ),
    "ppro": _apm_config(
        "grpc-apm-ppro",
        {
            "initialize": ("/sale", "POST"),
            "sale": ("/status", "GET"),
            "refund": ("/refund", "POST"),
        },
    ),
    "volt": _apm_config(
        "grpc-apm-volt",
        {
            "initialize": ("/dropin/payments", "POST"),
            "sale": ("/payments/{id}", "GET"),
            "refund": ("/payments/{id}/requests", "POST"),
        },
        extra_methods={
            "completion": {"proto_request": "SaleRequest", "proto_response": "SaleResponse"},
            "payout": {"proto_request": "RefundRequest", "proto_response": "RefundResponse"},
        },
    ),
    "paysafe": _apm_config(
        "grpc-providers-paysafe",
        {
            "initialize": ("/payments", "POST"),
            "sale": ("/payments/{id}", "GET"),
            "refund": ("/payments/{id}/refunds", "POST"),
        },
        extra_methods={
            "authorization": {"proto_request": "SaleRequest", "proto_response": "SaleResponse"},
            "cancellation": {"proto_request": "SaleRequest", "proto_response": "SaleResponse"},
            "completion": {"proto_request": "SaleRequest", "proto_response": "SaleResponse"},
            "verification": {"proto_request": "InitializeRequest", "proto_response": "InitializeResponse"},
            "payout": {"proto_request": "RefundRequest", "proto_response": "RefundResponse"},
        },
    ),
    "paynearme": _apm_config(
        "grpc-apm-paynearme",
        {
            "initialize": ("/create_order", "POST"),
            "sale": ("/status", "GET"),
        },
    ),
}


def find_proto_file(provider_config: dict) -> Path | None:
    """Find the first existing proto file from the config."""
    for rel in provider_config["proto_files"]:
        p = WORK_DIR / rel
        if p.exists():
            return p
    return None


def build_field_mappings_initialize(
    field_usages: list[FieldUsage],
    proto_msg_fields: list[str],
) -> tuple[list[FieldMapping], list[FieldMapping], list[str]]:
    """Build field mappings for the initialize method based on extracted JS patterns."""
    request_mappings: list[FieldMapping] = []
    response_mappings: list[FieldMapping] = []
    type_gaps: list[str] = []

    # Known mappings from reading the actual initialize.js + get-initialize-payload.js
    known_request_maps = {
        # Proto field -> Payper API field
        "email": FieldMapping(proto_field="consumerDetails.email", js_field="email", direction="request"),
        "phone": FieldMapping(
            proto_field="consumerDetails.phone", js_field="phone", direction="request", transform="prefix +"
        ),
        "first_name": FieldMapping(
            proto_field="consumerDetails.firstName",
            js_field="first_name",
            direction="request",
            transform="sanitizeAndCutInput(255)",
        ),
        "last_name": FieldMapping(
            proto_field="consumerDetails.lastName",
            js_field="last_name",
            direction="request",
            transform="sanitizeAndCutInput(255)",
        ),
        "address": FieldMapping(
            proto_field="billingDetails.addressLine",
            js_field="address",
            direction="request",
            transform="sanitizeAndCutInput(255)",
        ),
        "city": FieldMapping(
            proto_field="billingDetails.city",
            js_field="city",
            direction="request",
            transform="sanitizeAndCutInput(255)",
        ),
        "state": FieldMapping(proto_field="billingDetails.state", js_field="state", direction="request"),
        "country": FieldMapping(proto_field="billingDetails.countryAlpha2", js_field="country", direction="request"),
        "zip_code": FieldMapping(proto_field="billingDetails.zip", js_field="zip_code", direction="request"),
        "ip_address": FieldMapping(proto_field="metadata.ipAddress", js_field="ip_address", direction="request"),
        "udfs": FieldMapping(
            proto_field="identifiers.transactionId+attempt",
            js_field="udfs",
            direction="request",
            transform="[`${transactionId}aid${attempt}`]",
        ),
        "ntf_url": FieldMapping(proto_field="(env)WEBHOOKS_URL", js_field="ntf_url", direction="request"),
        "return_url": FieldMapping(
            proto_field="(env)CALLBACK_URL+identifiers+metadata.jwt",
            js_field="return_url",
            direction="request",
            transform="template string",
        ),
        "items": FieldMapping(
            proto_field="amount", js_field="items[0].unit_price", direction="request", transform="parseFloat"
        ),
    }

    known_response_maps_success = {
        "processor_transaction_id": FieldMapping(
            proto_field="data.processor_transaction_id", js_field="body.txid", direction="response"
        ),
        "redirect_url": FieldMapping(
            proto_field="data.redirect_url", js_field="body.bank_payment_url", direction="response"
        ),
    }

    known_response_maps_failure = {
        "transaction_status": FieldMapping(
            proto_field="data.transaction_status", js_field="TRANSACTION_STATUSES.DECLINED", direction="response"
        ),
        "issuer_response_code": FieldMapping(
            proto_field="data.issuer_response_code",
            js_field="getProviderError(errors).issuerResponseCode",
            direction="response",
        ),
        "issuer_response_text": FieldMapping(
            proto_field="data.issuer_response_text",
            js_field="getProviderError(errors).issuerResponseText",
            direction="response",
        ),
    }

    request_mappings = list(known_request_maps.values())
    response_mappings = list(known_response_maps_success.values()) + list(known_response_maps_failure.values())

    # Identify type gaps: proto fields that have no JS mapping
    mapped_proto_fields = {rm.proto_field.split(".")[0] for rm in request_mappings}
    for pf in proto_msg_fields:
        if pf not in mapped_proto_fields and pf not in ("identifiers", "authenticationData", "attempt", "metadata"):
            type_gaps.append(f"Proto field '{pf}' has no explicit JS mapping in initialize")

    # Response type gap: response is JSON.stringify'd to a string, losing all typing
    type_gaps.append("InitializeResponse.data is string (JSON blob) — all response fields lose proto typing")

    return request_mappings, response_mappings, type_gaps


def build_field_mappings_sale(
    field_usages: list[FieldUsage],
    proto_msg_fields: list[str],
) -> tuple[list[FieldMapping], list[FieldMapping], list[str]]:
    """Build field mappings for the sale method."""
    request_mappings: list[FieldMapping] = []
    response_mappings: list[FieldMapping] = []
    type_gaps: list[str] = []

    # Sale does a GET /status/{processorTransactionId} — no request payload
    request_mappings = [
        FieldMapping(proto_field="processorTransactionId", js_field="path parameter", direction="request"),
        FieldMapping(proto_field="authenticationData", js_field="Bearer token", direction="request"),
    ]

    # Response mapping from map-response.js
    response_mappings = [
        FieldMapping(
            proto_field="transactionStatus",
            js_field="statusesMap[response.status]",
            direction="response",
            transform="status enum mapping",
        ),
        FieldMapping(
            proto_field="processorTransactionId", js_field="processorTransactionId (passthrough)", direction="response"
        ),
        FieldMapping(
            proto_field="approvedAmount",
            js_field="response.amount",
            direction="response",
            transform="conditional on approved",
        ),
        FieldMapping(
            proto_field="finalize.issuerResponseCode", js_field="getProviderError(errors) or '00'", direction="response"
        ),
        FieldMapping(
            proto_field="finalize.issuerResponseText",
            js_field="getProviderError(errors) or 'Approved or completed successfully'",
            direction="response",
        ),
        FieldMapping(proto_field="finalize.resultSource", js_field="'payper' (constant)", direction="response"),
        FieldMapping(proto_field="finalize.result", js_field="transactionStatus", direction="response"),
        FieldMapping(proto_field="finalize.timestamp", js_field="new Date().toISOString()", direction="response"),
        FieldMapping(
            proto_field="finalize.providerErrorCode",
            js_field="response.errors[0].code",
            direction="response",
            transform="conditional on declined",
        ),
        FieldMapping(
            proto_field="finalize.providerErrorMessage",
            js_field="response.errors[0].message",
            direction="response",
            transform="conditional on declined",
        ),
        FieldMapping(proto_field="paymentMethod.type", js_field="'generic' (constant)", direction="response"),
        FieldMapping(
            proto_field="paymentMethod.uniqueIdentifier",
            js_field="'interac:' + (email||responseEmail||processorTransactionId)",
            direction="response",
        ),
        FieldMapping(proto_field="paymentMethod.generic.type", js_field="'interac' (constant)", direction="response"),
        FieldMapping(
            proto_field="paymentMethod.generic.details.senderBank",
            js_field="response.sender_bank",
            direction="response",
            transform="conditional",
        ),
        FieldMapping(
            proto_field="paymentMethod.generic.details.senderName",
            js_field="response.sender_name",
            direction="response",
            transform="conditional",
        ),
        FieldMapping(
            proto_field="paymentMethod.generic.details.phone",
            js_field="response.phone",
            direction="response",
            transform="conditional",
        ),
        FieldMapping(
            proto_field="metadata.failureMessage",
            js_field="providerErrorMessage",
            direction="response",
            transform="conditional on error",
        ),
        FieldMapping(
            proto_field="metadata.failureCode",
            js_field="providerErrorCode",
            direction="response",
            transform="conditional on error",
        ),
    ]

    # Type gaps
    unmapped = {
        "currency",
        "amount",
        "consumer",
        "billingDetails",
        "threeDs",
        "currencyExponent",
        "transactionSubType",
        "paymentMethodOptions",
    }
    for pf in proto_msg_fields:
        if pf in unmapped:
            type_gaps.append(f"Proto field '{pf}' sent to provider but unused (sale is GET /status only)")

    type_gaps.append("Sale does no request body — most SaleRequest fields are unused by Payper")
    type_gaps.append("Response fields (sender_bank, sender_name, phone) are untyped strings from Payper JSON")

    return request_mappings, response_mappings, type_gaps


def build_field_mappings_refund(
    field_usages: list[FieldUsage],
    proto_msg_fields: list[str],
) -> tuple[list[FieldMapping], list[FieldMapping], list[str]]:
    """Build field mappings for the refund method."""
    request_mappings = [
        FieldMapping(proto_field="processorTransactionId", js_field="txid", direction="request"),
        FieldMapping(proto_field="amount", js_field="amount", direction="request", transform="parseFloat"),
        FieldMapping(
            proto_field="identifiers.transactionId",
            js_field="udfs[0]",
            direction="request",
            transform="`${transactionId}aid1`",
        ),
        FieldMapping(proto_field="authenticationData", js_field="Bearer token (header)", direction="request"),
    ]

    response_mappings = [
        FieldMapping(
            proto_field="transactionStatus",
            js_field="statusesMap.refund[response.status]",
            direction="response",
            transform="refund status enum",
        ),
        FieldMapping(
            proto_field="processorTransactionId", js_field="processorTransactionId (passthrough)", direction="response"
        ),
        FieldMapping(
            proto_field="finalize.issuerResponseCode", js_field="getProviderError(errors) or '00'", direction="response"
        ),
        FieldMapping(
            proto_field="finalize.issuerResponseText",
            js_field="getProviderError(errors) or 'Approved'",
            direction="response",
        ),
        FieldMapping(proto_field="finalize.resultSource", js_field="'payper' (constant)", direction="response"),
        FieldMapping(proto_field="finalize.result", js_field="transactionStatus", direction="response"),
        FieldMapping(proto_field="finalize.timestamp", js_field="new Date().toISOString()", direction="response"),
        FieldMapping(
            proto_field="metadata.failureMessage",
            js_field="providerErrorMessage",
            direction="response",
            transform="conditional",
        ),
        FieldMapping(
            proto_field="metadata.failureCode",
            js_field="providerErrorCode",
            direction="response",
            transform="conditional",
        ),
    ]

    type_gaps: list[str] = []
    unmapped = {
        "paymentMethod",
        "currency",
        "currencyExponent",
        "processorTransactionTimestamp",
        "refundedTransactionType",
        "consumerId",
        "consumer",
        "descriptor",
        "partialRefund",
    }
    for pf in proto_msg_fields:
        if pf in unmapped:
            type_gaps.append(f"Proto field '{pf}' not used in Payper refund request")

    type_gaps.append("Refund response uses same mapResponse as sale — no paymentMethod in refund response")
    type_gaps.append("Refund status mapping differs: 'approved' -> PENDING (not APPROVED)")

    return request_mappings, response_mappings, type_gaps


def build_field_mappings_payout(
    field_usages: list[FieldUsage],
    proto_msg_fields: list[str],
) -> tuple[list[FieldMapping], list[FieldMapping], list[str]]:
    """Build field mappings for the payout method."""
    request_mappings = [
        FieldMapping(proto_field="amount", js_field="amount", direction="request", transform="parseFloat"),
        FieldMapping(proto_field="consumer.email", js_field="email", direction="request"),
        FieldMapping(
            proto_field="consumer.firstName",
            js_field="first_name",
            direction="request",
            transform="sanitizeAndCutInput(255)",
        ),
        FieldMapping(
            proto_field="consumer.lastName",
            js_field="last_name",
            direction="request",
            transform="sanitizeAndCutInput(255)",
        ),
        FieldMapping(proto_field="consumer.ipAddress", js_field="ip_address", direction="request"),
        FieldMapping(
            proto_field="billingDetails.addressLine",
            js_field="address",
            direction="request",
            transform="sanitizeAndCutInput(255)",
        ),
        FieldMapping(
            proto_field="billingDetails.city",
            js_field="city",
            direction="request",
            transform="sanitizeAndCutInput(255)",
        ),
        FieldMapping(proto_field="billingDetails.state", js_field="state", direction="request"),
        FieldMapping(proto_field="billingDetails.countryAlpha2", js_field="country", direction="request"),
        FieldMapping(proto_field="billingDetails.zip", js_field="zip_code", direction="request"),
        FieldMapping(
            proto_field="identifiers.transactionId",
            js_field="udfs[0]",
            direction="request",
            transform="`${transactionId}aid1`",
        ),
        FieldMapping(proto_field="(env)WEBHOOKS_URL", js_field="ntf_url", direction="request"),
        FieldMapping(proto_field="authenticationData", js_field="Bearer token (header)", direction="request"),
    ]

    response_mappings = [
        FieldMapping(
            proto_field="transactionStatus", js_field="statusesMap.payout[response.status]", direction="response"
        ),
        FieldMapping(proto_field="processorTransactionId", js_field="body.txid", direction="response"),
        FieldMapping(
            proto_field="finalize.issuerResponseCode",
            js_field="getProviderError(errors).issuerResponseCode",
            direction="response",
        ),
        FieldMapping(
            proto_field="finalize.issuerResponseText",
            js_field="getProviderError(errors).issuerResponseText",
            direction="response",
        ),
        FieldMapping(proto_field="finalize.resultSource", js_field="'payper' (constant)", direction="response"),
        FieldMapping(proto_field="finalize.result", js_field="transactionStatus", direction="response"),
        FieldMapping(proto_field="finalize.timestamp", js_field="new Date().toISOString()", direction="response"),
        FieldMapping(
            proto_field="metadata.failureMessage",
            js_field="providerErrorMessage",
            direction="response",
            transform="conditional",
        ),
        FieldMapping(
            proto_field="metadata.failureCode",
            js_field="providerErrorCode",
            direction="response",
            transform="conditional",
        ),
    ]

    type_gaps: list[str] = [
        "CRITICAL: consumer.phone NOT in PayoutConsumerDetails proto — Payper API REQUIRES phone for payout",
        "phone available via paymentMethod.additionalInfo.phone (common.proto field 10) — NOT read by payload builder",
        "Payout reuses same mapResponse as sale/refund — context='payout' only changes status mapping",
    ]

    return request_mappings, response_mappings, type_gaps


def build_field_mappings_generic(
    field_usages: list[FieldUsage],
    proto_msg_fields: list[str],
    method_name: str,
) -> tuple[list[FieldMapping], list[FieldMapping], list[str]]:
    """Generic fallback for methods without specific builders. Extracts from JS field usages."""
    request_mappings: list[FieldMapping] = []
    response_mappings: list[FieldMapping] = []
    type_gaps: list[str] = []

    # Find usages that look like request destructuring
    for usage in field_usages:
        if usage.usage_type == "destructure" and method_name in usage.file_path.lower():
            request_mappings.append(
                FieldMapping(
                    proto_field=usage.field_name,
                    js_field=usage.target_field or usage.field_name,
                    direction="request",
                )
            )
        elif usage.usage_type == "response_map" and method_name in usage.file_path.lower():
            response_mappings.append(
                FieldMapping(
                    proto_field=usage.field_name,
                    js_field=usage.target_field or usage.field_name,
                    direction="response",
                )
            )

    if not request_mappings and not response_mappings:
        type_gaps.append(f"No field mappings auto-extracted for '{method_name}' — manual review needed")

    return request_mappings, response_mappings, type_gaps


def build_provider_type_map(provider: str) -> ProviderTypeMap:
    """Build the complete type map for a provider."""
    if provider not in PROVIDER_CONFIGS:
        raise ValueError(f"Unknown provider: {provider}. Known: {list(PROVIDER_CONFIGS.keys())}")

    config = PROVIDER_CONFIGS[provider]

    # Parse proto
    proto_path = find_proto_file(config)
    if proto_path is None:
        raise FileNotFoundError(f"No proto file found for {provider}. Searched: {config['proto_files']}")

    schema = parse_proto_path(proto_path, source_repo=config["repo_name"])

    # Also try to parse the common proto (providers.proto) for FinalizeProviders etc
    providers_proto = WORK_DIR / "libs-types" / "protos" / "providers.proto"
    if providers_proto.exists():
        extra_schema = parse_proto_path(providers_proto, source_repo="libs-types")
        schema = merge_schemas(schema, extra_schema)

    # Extract JS fields
    methods_dir = WORK_DIR / config["methods_dir"]
    libs_dir = WORK_DIR / config["libs_dir"]

    all_usages: list[FieldUsage] = []
    if methods_dir.exists():
        all_usages.extend(extract_fields_from_directory(str(methods_dir)))
    if libs_dir.exists():
        all_usages.extend(extract_fields_from_directory(str(libs_dir)))

    # Build per-method type maps
    methods: dict[str, MethodTypeMap] = {}

    for method_name, method_cfg in config["method_config"].items():
        req_msg_name = method_cfg["proto_request"]
        resp_msg_name = method_cfg["proto_response"]

        req_msg = schema.messages.get(req_msg_name)
        proto_fields = [f.name for f in req_msg.fields] if req_msg else []

        # Method-specific field mapping builders
        if method_name == "initialize":
            req_maps, resp_maps, gaps = build_field_mappings_initialize(all_usages, proto_fields)
        elif method_name == "sale":
            req_maps, resp_maps, gaps = build_field_mappings_sale(all_usages, proto_fields)
        elif method_name == "refund":
            req_maps, resp_maps, gaps = build_field_mappings_refund(all_usages, proto_fields)
        elif method_name == "payout":
            req_maps, resp_maps, gaps = build_field_mappings_payout(all_usages, proto_fields)
        else:
            req_maps, resp_maps, gaps = build_field_mappings_generic(all_usages, proto_fields, method_name)

        methods[method_name] = MethodTypeMap(
            method=method_name,
            proto_request=req_msg_name,
            proto_response=resp_msg_name,
            request_fields=req_maps,
            response_fields=resp_maps,
            api_endpoint=method_cfg["api_endpoint"],
            api_method=method_cfg["api_method"],
            type_gaps=gaps,
        )

    return ProviderTypeMap(
        provider=provider,
        proto_service=config["service_name"],
        methods=methods,
        field_usages=all_usages,
    )


def type_map_to_yaml(type_map: ProviderTypeMap) -> str:
    """Serialize a ProviderTypeMap to YAML for human consumption."""
    data = {
        "provider": type_map.provider,
        "proto_service": type_map.proto_service,
        "methods": {},
        "summary": {
            "total_field_usages": len(type_map.field_usages),
            "total_type_gaps": sum(len(m.type_gaps) for m in type_map.methods.values()),
        },
    }

    for name, mtm in type_map.methods.items():
        method_data: dict = {
            "proto_request": mtm.proto_request,
            "proto_response": mtm.proto_response,
            "api_endpoint": mtm.api_endpoint,
            "api_method": mtm.api_method,
            "request_field_mappings": [],
            "response_field_mappings": [],
            "type_gaps": mtm.type_gaps,
        }

        for fm in mtm.request_fields:
            entry: dict = {"proto": fm.proto_field, "js": fm.js_field}
            if fm.transform:
                entry["transform"] = fm.transform
            method_data["request_field_mappings"].append(entry)

        for fm in mtm.response_fields:
            entry = {"proto": fm.proto_field, "js": fm.js_field}
            if fm.transform:
                entry["transform"] = fm.transform
            method_data["response_field_mappings"].append(entry)

        data["methods"][name] = method_data

    return yaml.dump(data, default_flow_style=False, sort_keys=False, width=120, allow_unicode=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build shadow type maps for a provider")
    parser.add_argument("--provider", required=True, help="Provider name (e.g. payper)")
    parser.add_argument("--method", default="", help="Specific method to show (optional)")
    parser.add_argument("--output-dir", default="", help="Output directory (default: profiles/pay-com/provider_types/)")
    args = parser.parse_args()

    type_map = build_provider_type_map(args.provider)

    # If specific method requested, filter
    if args.method:
        if args.method not in type_map.methods:
            print(f"Error: method '{args.method}' not found. Available: {list(type_map.methods.keys())}")
            sys.exit(1)
        type_map.methods = {args.method: type_map.methods[args.method]}

    yaml_output = type_map_to_yaml(type_map)

    # Determine output path
    if args.output_dir:
        out_dir = Path(args.output_dir)
    else:
        out_dir = PROJECT_ROOT / "profiles" / "pay-com" / "provider_types"

    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"{args.provider}.yaml"
    out_file.write_text(yaml_output, encoding="utf-8")
    print(f"Written: {out_file}")
    print("\nSummary:")
    print(f"  Methods: {len(type_map.methods)}")
    print(f"  Field usages extracted: {len(type_map.field_usages)}")
    print(f"  Type gaps found: {sum(len(m.type_gaps) for m in type_map.methods.values())}")
    print("\n--- YAML preview ---\n")
    # Print first 80 lines
    lines = yaml_output.split("\n")
    for line in lines[:80]:
        print(line)
    if len(lines) > 80:
        print(f"\n... ({len(lines) - 80} more lines, see {out_file})")


if __name__ == "__main__":
    main()
