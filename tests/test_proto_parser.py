"""Tests for proto_parser — validates parsing of .proto files."""

from src.proto_parser import merge_schemas, parse_proto_file


SAMPLE_PROTO = """
syntax = "proto3";

package provider;

import "types/protos/common.proto";

service ProviderService {
  rpc sale (SaleRequest) returns (SaleResponse) {};
  rpc refund (RefundRequest) returns (RefundResponse) {};
  rpc initialize (InitializeRequest) returns (InitializeResponse) {};
}

enum MitReasonEnum {
  FIRST = 0;
  SUBSEQUENT = 1;
  UNSCHEDULED = 2;
}

enum TransactionStatusEnum {
  APPROVED = 0;
  PENDING = 1;
  DECLINED = 2;
}

message SaleConsumerDetails {
  optional string ipAddress = 1;
  optional string email = 2;
  optional string firstName = 3;
  optional string lastName = 4;
}

message SaleRequest {
  required string authenticationData = 1;
  required string currency = 3;
  required string amount = 5;
  optional string threeDs = 6;
  required SaleConsumerDetails consumer = 7;
  required string currencyExponent = 8;
  optional bool mit = 10;
  optional MitReasonEnum mitReason = 11;
  map<string, string> metadata = 27;
}

message SaleResponse {
  required string processorTransactionId = 1;
  required TransactionStatusEnum transactionStatus = 2;
  optional string networkTransactionId = 4;
  repeated string fees = 6;
  optional string approvedAmount = 7;
  map<string, string> metadata = 8;
}

message InitializeRequest {
  required string identifiers = 1;
  optional string amount = 2;
  optional string currency = 3;
  required string authenticationData = 4;
  required string consumerId = 5;
}

message InitializeResponse {
  required string type = 1;
  required string data = 2;
  map<string, string> metadata = 3;
}

message RefundRequest {
  required string authenticationData = 1;
  required string amount = 2;
  required string processorTransactionId = 6;
}

message RefundResponse {
  required TransactionStatusEnum transactionStatus = 1;
  required string processorTransactionId = 2;
  map<string, string> metadata = 5;
}
"""


class TestProtoParser:
    def test_parse_messages(self):
        schema = parse_proto_file(SAMPLE_PROTO, "test.proto", "test-repo")
        assert "SaleRequest" in schema.messages
        assert "SaleResponse" in schema.messages
        assert "InitializeRequest" in schema.messages
        assert "InitializeResponse" in schema.messages
        assert "RefundRequest" in schema.messages
        assert "RefundResponse" in schema.messages
        assert "SaleConsumerDetails" in schema.messages

    def test_message_fields(self):
        schema = parse_proto_file(SAMPLE_PROTO)
        req = schema.messages["SaleRequest"]
        field_names = [f.name for f in req.fields]
        assert "authenticationData" in field_names
        assert "currency" in field_names
        assert "amount" in field_names
        assert "metadata" in field_names

    def test_field_attributes(self):
        schema = parse_proto_file(SAMPLE_PROTO)
        req = schema.messages["SaleRequest"]
        fields_by_name = {f.name: f for f in req.fields}

        auth = fields_by_name["authenticationData"]
        assert auth.type == "string"
        assert auth.number == 1
        assert auth.optional is False

        threeDs = fields_by_name["threeDs"]
        assert threeDs.optional is True

        mit = fields_by_name["mit"]
        assert mit.type == "bool"
        assert mit.optional is True

    def test_map_field(self):
        schema = parse_proto_file(SAMPLE_PROTO)
        req = schema.messages["SaleRequest"]
        fields_by_name = {f.name: f for f in req.fields}
        meta = fields_by_name["metadata"]
        assert meta.type == "map<string, string>"

    def test_repeated_field(self):
        schema = parse_proto_file(SAMPLE_PROTO)
        resp = schema.messages["SaleResponse"]
        fields_by_name = {f.name: f for f in resp.fields}
        fees = fields_by_name["fees"]
        assert fees.repeated is True

    def test_parse_services(self):
        schema = parse_proto_file(SAMPLE_PROTO)
        assert "ProviderService" in schema.services
        svc = schema.services["ProviderService"]
        rpc_names = [r.name for r in svc.rpcs]
        assert "sale" in rpc_names
        assert "refund" in rpc_names
        assert "initialize" in rpc_names

    def test_rpc_types(self):
        schema = parse_proto_file(SAMPLE_PROTO)
        svc = schema.services["ProviderService"]
        rpcs_by_name = {r.name: r for r in svc.rpcs}
        sale = rpcs_by_name["sale"]
        assert sale.request_type == "SaleRequest"
        assert sale.response_type == "SaleResponse"

    def test_parse_enums(self):
        schema = parse_proto_file(SAMPLE_PROTO)
        assert "MitReasonEnum" in schema.enums
        mit_enum = schema.enums["MitReasonEnum"]
        assert "FIRST" in mit_enum.values
        assert "SUBSEQUENT" in mit_enum.values
        assert "UNSCHEDULED" in mit_enum.values

        assert "TransactionStatusEnum" in schema.enums
        status_enum = schema.enums["TransactionStatusEnum"]
        assert len(status_enum.values) == 3

    def test_source_metadata(self):
        schema = parse_proto_file(SAMPLE_PROTO, "path/to/service.proto", "my-repo")
        msg = schema.messages["SaleRequest"]
        assert msg.source_file == "path/to/service.proto"
        assert msg.source_repo == "my-repo"

    def test_merge_schemas(self):
        schema1 = parse_proto_file(SAMPLE_PROTO, "file1.proto", "repo1")
        extra_proto = """
message FinalizeProviders {
  optional string authCode = 1;
  optional string rrn = 2;
}

enum PayoutType {
  INSTANT = 0;
  STANDARD = 1;
}
"""
        schema2 = parse_proto_file(extra_proto, "file2.proto", "repo2")
        merged = merge_schemas(schema1, schema2)

        # Should have messages from both
        assert "SaleRequest" in merged.messages
        assert "FinalizeProviders" in merged.messages
        assert "MitReasonEnum" in merged.enums
        assert "PayoutType" in merged.enums

    def test_empty_message(self):
        proto = "message EmptyMsg {}"
        schema = parse_proto_file(proto)
        assert "EmptyMsg" in schema.messages
        assert schema.messages["EmptyMsg"].fields == []

    def test_empty_content(self):
        schema = parse_proto_file("")
        assert schema.messages == {}
        assert schema.services == {}
        assert schema.enums == {}
