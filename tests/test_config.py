"""Tests for config.py — constants and glossary."""

from src.config import (
    BASE_DIR,
    DB_PATH,
    DICTIONARY_ALIAS_MAP,
    DICTIONARY_HINT_MAP,
    DOMAIN_GLOSSARY,
    FLOW_EDGE_TYPES,
    KNOWN_FLOWS,
    LANCE_PATH,
)


class TestPaths:
    def test_base_dir_exists(self):
        # BASE_DIR defaults to ~/.code-rag, overridable via CODE_RAG_HOME
        assert BASE_DIR.is_dir() or BASE_DIR.name in (".code-rag", ".code-rag-mcp")

    def test_db_path_under_base(self):
        assert str(DB_PATH).startswith(str(BASE_DIR))
        assert DB_PATH.name == "knowledge.db"

    def test_lance_path_under_base(self):
        assert str(LANCE_PATH).startswith(str(BASE_DIR))
        assert "vectors.lance" in str(LANCE_PATH)


class TestDomainGlossary:
    def test_not_empty(self):
        assert len(DOMAIN_GLOSSARY) > 20

    def test_keys_are_lowercase(self):
        for key in DOMAIN_GLOSSARY:
            assert key == key.lower(), f"Key '{key}' should be lowercase"

    def test_common_abbreviations_present(self):
        expected = ["nt", "3ds", "apm", "ddm", "pci", "ach"]
        for abbr in expected:
            assert abbr in DOMAIN_GLOSSARY, f"'{abbr}' missing from glossary"

    def test_values_are_non_empty_strings(self):
        for key, val in DOMAIN_GLOSSARY.items():
            assert isinstance(val, str) and len(val) > 0, f"Empty value for '{key}'"


class TestFlowEdgeTypes:
    def test_not_empty(self):
        assert len(FLOW_EDGE_TYPES) >= 5

    def test_grpc_call_present(self):
        assert "grpc_call" in FLOW_EDGE_TYPES

    def test_grpc_client_usage_present(self):
        assert "grpc_client_usage" in FLOW_EDGE_TYPES

    def test_no_npm_dep(self):
        # npm_dep is noise, should not be in flow edges
        assert "npm_dep" not in FLOW_EDGE_TYPES


class TestKnownFlows:
    def test_not_empty(self):
        assert len(KNOWN_FLOWS) >= 8

    def test_payment_flow_exists(self):
        assert "payment" in KNOWN_FLOWS
        assert len(KNOWN_FLOWS["payment"]) > 0

    def test_all_flows_have_seeds(self):
        for name, seeds in KNOWN_FLOWS.items():
            assert len(seeds) > 0, f"Flow '{name}' has no seed repos"


class TestDictionary:
    def test_alias_map_not_empty(self):
        assert len(DICTIONARY_ALIAS_MAP) > 0

    def test_hint_map_not_empty(self):
        assert len(DICTIONARY_HINT_MAP) > 0

    def test_auth_code_alias(self):
        # fields.yaml: authCode aliases: [auth_code]
        assert "auth_code" in DICTIONARY_ALIAS_MAP
        assert "authcode" in DICTIONARY_ALIAS_MAP
        assert "authCode" in DICTIONARY_ALIAS_MAP["auth_code"]
        assert "auth_code" in DICTIONARY_ALIAS_MAP["authcode"]

    def test_hint_for_auth_code(self):
        assert "authcode" in DICTIONARY_HINT_MAP
        assert "auth_code" in DICTIONARY_HINT_MAP
        assert "authorization code" in DICTIONARY_HINT_MAP["authcode"].lower()

    def test_concept_present(self):
        assert "throw_policy" in DICTIONARY_ALIAS_MAP
        assert "throw_policy" in DICTIONARY_HINT_MAP

    def test_entity_present(self):
        assert "transaction" in DICTIONARY_ALIAS_MAP
        assert "transaction" in DICTIONARY_HINT_MAP
