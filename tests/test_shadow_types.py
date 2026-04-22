"""Tests for shadow type layer — JS field extraction and MCP tool."""

import pytest

from src.js_field_extractor import extract_fields_from_file
from src.tools.shadow_types import provider_type_map_tool
from src.types import (
    FieldMapping,
    FieldUsage,
    MethodTypeMap,
    ProtoEnum,
    ProtoField,
    ProtoMessage,
    ProtoSchema,
    ProviderTypeMap,
)

# ---------------------------------------------------------------------------
# JS Field Extractor Tests
# ---------------------------------------------------------------------------

SAMPLE_INITIALIZE_JS = """
const { makeApiCall, getCredentials } = require('../libs')
const { getInitializePayload } = require('../libs/payload-builders')

module.exports = async ({ req }) => {
  const {
    identifiers: { transactionId, companyId, merchantId },
    identifiers,
    authenticationData,
  } = req

  const payload = getInitializePayload(req)
  const credentials = getCredentials(authenticationData)

  const { body, isFailure } = await makeApiCall({
    path: '/payment/eTransfer/',
    payload,
    identifiers,
    credentials,
  })

  if (isFailure) {
    return {
      type: 'OBJECT',
      data: JSON.stringify({
        transaction_status: 'DECLINED',
        issuer_response_code: 'code',
      }),
    }
  }

  return {
    type: 'OBJECT',
    data: JSON.stringify({
      processor_transaction_id: body.txid,
      redirect_url: body.bank_payment_url,
    }),
  }
}
"""

module.exports = (request) => {
  const {
    identifiers: { transactionId },
    amount,
    attempt,
    consumerDetails: { email, phone, firstName, lastName } = {},
    billingDetails: { addressLine, city, state, countryAlpha2, zip } = {},
    metadata: { jwt, ipAddress } = {},
  } = request

  return {
    ...(email && { email }),
    ...(phone && { phone: phone.startsWith('+') ? phone : `+${phone}` }),
    ...(firstName && { first_name: sanitizeAndCutInput(firstName, 255) }),
    ...(lastName && { last_name: sanitizeAndCutInput(lastName, 255) }),
    ...(addressLine && { address: sanitizeAndCutInput(addressLine, 255) }),
    ...(city && { city: sanitizeAndCutInput(city, 255) }),
    ...(state && { state }),
    ...(countryAlpha2 && { country: countryAlpha2 }),
    ...(zip && { zip_code: zip }),
    ...(ipAddress && { ip_address: ipAddress }),
    udfs: [`${transactionId}aid${attempt}`],
    ntf_url: WEBHOOKS_URL,
    return_url: `${CALLBACK_URL}?status=success`,
    items: [
      {
        name: 'Online payment',
        quantity: 1,
        unit_price: parseFloat(amount),
      },
    ],
  }
}
"""

SAMPLE_MAP_RESPONSE_JS = """
module.exports = ({ response = {}, processorTransactionId, paymentMethod, isFailure, email }) => {
  const {
    status: providerStatus,
    amount: approvedAmount,
    sender_bank: senderBank,
    sender_name: senderName,
    phone: responsePhone,
    email: responseEmail,
    errors,
  } = response

  return {
    transactionStatus,
    processorTransactionId,
    ...(isApproved && { approvedAmount }),
    finalize: {
      processorTransactionId,
      issuerResponseCode,
      issuerResponseText,
      resultSource: PROVIDER,
    },
    ...(paymentMethod && {
      paymentMethod: {
        type: 'generic',
        uniqueIdentifier: email || responseEmail,
      },
    }),
    metadata: {
      ...(providerErrorMessage && { failureMessage: providerErrorMessage }),
      ...(providerErrorCode && { failureCode: providerErrorCode }),
    },
  }
}
"""

class TestJsFieldExtractor:
    def test_destructuring_nested(self):
        usages = extract_fields_from_file("test.js", SAMPLE_INITIALIZE_JS)
        destructures = [u for u in usages if u.usage_type == "destructure"]
        field_names = [u.field_name for u in destructures]
        assert "transactionId" in field_names
        assert "companyId" in field_names
        assert "merchantId" in field_names
        assert "authenticationData" in field_names

    def test_destructuring_from_response(self):
        usages = extract_fields_from_file("test.js", SAMPLE_INITIALIZE_JS)
        destructures = [u for u in usages if u.usage_type == "destructure"]
        field_names = [u.field_name for u in destructures]
        assert "body" in field_names
        assert "isFailure" in field_names

    def test_payload_build_fields(self):
        usages = extract_fields_from_file("test.js", SAMPLE_INITIALIZE_JS)
        payload_builds = [u for u in usages if u.usage_type == "payload_build"]
        field_names = [u.field_name for u in payload_builds]
        assert "path" in field_names
        assert "processor_transaction_id" in field_names
        assert "redirect_url" in field_names

    def test_conditional_fields(self):
        usages = extract_fields_from_file("test.js", SAMPLE_PAYLOAD_BUILDER_JS)
        conditionals = [u for u in usages if u.usage_type == "conditional"]
        field_names = [u.field_name for u in conditionals]
        assert "email" in field_names
        assert "phone" in field_names
        assert "first_name" in field_names
        assert "last_name" in field_names
        assert "address" in field_names
        assert "city" in field_names
        assert "state" in field_names
        assert "country" in field_names
        assert "zip_code" in field_names
        assert "ip_address" in field_names

    def test_conditional_is_optional(self):
        usages = extract_fields_from_file("test.js", SAMPLE_PAYLOAD_BUILDER_JS)
        conditionals = [u for u in usages if u.usage_type == "conditional"]
        for u in conditionals:
            assert u.is_optional is True

    def test_response_destructure(self):
        usages = extract_fields_from_file("test.js", SAMPLE_MAP_RESPONSE_JS)
        destructures = [u for u in usages if u.usage_type == "destructure"]
        field_names = [u.field_name for u in destructures]
        assert "response" in field_names or "providerStatus" in field_names or "status" in field_names

    def test_response_conditional_spread(self):
        usages = extract_fields_from_file("test.js", SAMPLE_MAP_RESPONSE_JS)
        conditionals = [u for u in usages if u.usage_type == "conditional"]
        field_names = [u.field_name for u in conditionals]
        assert "approvedAmount" in field_names
        assert "failureMessage" in field_names
        assert "failureCode" in field_names

class TestShadowTypeModels:
    def test_field_usage_model(self):
        fu = FieldUsage(
            field_name="email",
            file_path="test.js",
            usage_type="conditional",
            source_field="email",
            target_field="email",
            is_optional=True,
        )
        assert fu.field_name == "email"
        assert fu.is_optional is True

    def test_field_mapping_model(self):
        fm = FieldMapping(
            proto_field="consumerDetails.email",
            js_field="email",
            direction="request",
            transform="",
        )
        assert fm.proto_field == "consumerDetails.email"
        assert fm.direction == "request"

    def test_method_type_map_model(self):
        mtm = MethodTypeMap(
            method="initialize",
            proto_request="InitializeRequest",
            proto_response="InitializeResponse",
            api_endpoint="/payment/eTransfer/",
            api_method="POST",
            type_gaps=["field X has no mapping"],
        )
        assert mtm.method == "initialize"
        assert len(mtm.type_gaps) == 1

    def test_provider_type_map_model(self):
        ptm = ProviderTypeMap(
            provider="payper",
            proto_service="ProviderService",
            methods={
                "initialize": MethodTypeMap(
                    method="initialize",
                    proto_request="InitializeRequest",
                    proto_response="InitializeResponse",
                ),
            },
            field_usages=[
                FieldUsage(field_name="email", file_path="test.js", usage_type="destructure"),
            ],
        )
        assert ptm.provider == "payper"
        assert "initialize" in ptm.methods
        assert len(ptm.field_usages) == 1

    def test_proto_enum_model(self):
        pe = ProtoEnum(name="StatusEnum", values=["APPROVED", "DECLINED"])
        assert pe.name == "StatusEnum"
        assert len(pe.values) == 2

class TestProviderTypeMapTool:
    def test_missing_provider(self):
        result = provider_type_map_tool("nonexistent_provider_xyz")
        assert "not indexed" in result.lower()

    def test_fields_mode_no_method(self):
        result = provider_type_map_tool("nonexistent_provider_xyz", method="", mode="fields")
        assert "nonexistent_provider_xyz" in result

    def test_fields_mode_no_method_valid_provider(self, tmp_path, monkeypatch):
        import src.tools.shadow_types as st

        yaml_path = tmp_path / "fake.yaml"
        yaml_path.write_text(
            "provider: fake\nproto_service: FakeService\nmethods:\n  sale:\n    proto_request: SaleRequest\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(st, "_PROVIDER_TYPES_DIR", tmp_path)
        result = st.provider_type_map_tool("fake", method="", mode="fields")
        assert result == "Error: mode='fields' requires non-empty method"
