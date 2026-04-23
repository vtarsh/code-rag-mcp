"""Tests for chunking functions in scripts/build_index.py."""

import sys
from pathlib import Path

# Ensure scripts/ is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from build_index import (
    MAX_CHUNK,
    MIN_CHUNK,
    chunk_code,
    chunk_markdown,
    chunk_proto,
)


class TestChunkProto:
    """Proto file chunking by service/message/enum definition."""

    def test_splits_on_message_boundary(self):
        content = (
            "syntax = 'proto3';\n\n"
            "message PaymentRequest {\n  string id = 1;\n  string amount = 2;\n}\n\n"
            "message PaymentResponse {\n  string status = 1;\n  string transaction_id = 2;\n}\n"
        )
        chunks = chunk_proto(content, "test-repo")
        assert len(chunks) >= 2
        # Each chunk should have repo prefix
        for c in chunks:
            assert c["content"].startswith("[Repo: test-repo]")

    def test_splits_on_service_boundary(self):
        # Need enough content per section to exceed MIN_CHUNK (50 chars)
        content = (
            "syntax = 'proto3';\npackage payments;\nimport 'common.proto';\n\n"
            "service PaymentService {\n  rpc Create(PaymentRequest) returns (PaymentResponse);\n"
            "  rpc Get(GetPaymentRequest) returns (PaymentResponse);\n"
            "  rpc List(ListRequest) returns (ListResponse);\n}\n\n"
            "message PaymentRequest {\n  string id = 1;\n  string amount = 2;\n"
            "  string currency = 3;\n  string description = 4;\n}\n"
        )
        chunks = chunk_proto(content, "test-repo")
        assert len(chunks) >= 2
        types = [c["chunk_type"] for c in chunks]
        assert any("service" in t for t in types)
        assert any("message" in t for t in types)

    def test_small_file_single_chunk(self):
        content = "syntax = 'proto3';\npackage test;\nmessage Tiny { string x = 1; }"
        chunks = chunk_proto(content, "test-repo")
        # Should produce at least one chunk (file is above MIN_CHUNK)
        assert len(chunks) >= 1

    def test_empty_content(self):
        chunks = chunk_proto("", "test-repo")
        assert chunks == []

    def test_below_min_chunk(self):
        content = "short"
        chunks = chunk_proto(content, "test-repo")
        assert chunks == []

    def test_chunk_type_labels(self):
        content = (
            "syntax = 'proto3';\npackage test;\n\n"
            "enum Status {\n  UNKNOWN = 0;\n  ACTIVE = 1;\n  DECLINED = 2;\n}\n\n"
            "message PaymentRequest {\n  string id = 1;\n  Status status = 2;\n}\n"
        )
        chunks = chunk_proto(content, "test-repo")
        types = [c["chunk_type"] for c in chunks]
        assert any("enum" in t for t in types)
        assert any("message" in t for t in types)


class TestChunkMarkdown:
    """Markdown chunking by header sections."""

    def test_splits_on_headers(self):
        # Each section body (after repo prefix) must exceed MIN_DOC_BODY (120 chars)
        content = (
            "# Introduction\n\nThe payment processing service exposes multiple gRPC endpoints for card "
            "transactions, refunds, and reversals that clients depend on for reliable settlement flows.\n\n"
            "## Details\n\nDetailed description of the feature covering request validation, provider routing, "
            "idempotency keys, retry semantics, and the downstream webhook notification contracts to users.\n"
        )
        chunks = chunk_markdown(content, "test-repo")
        assert len(chunks) >= 2
        for c in chunks:
            assert c["chunk_type"] == "doc_section"

    def test_single_section(self):
        content = (
            "# Only One Section\n\nThis is the only section with enough substantive body text to pass the "
            "new 120-character minimum body threshold used by the orphan-heading filter introduced in the "
            "chunker audit fix; it describes service behavior in meaningful detail.\n"
        )
        chunks = chunk_markdown(content, "test-repo")
        assert len(chunks) >= 1

    def test_no_headers_still_chunks(self):
        content = (
            "This is a markdown file without any headers but with enough substantive body content to be "
            "indexed as a chunk even after the new body-only 120-character orphan-heading filter takes "
            "effect across the doc_section and doc_file paths in chunk_markdown."
        )
        chunks = chunk_markdown(content, "test-repo")
        assert len(chunks) == 1
        # Without headers, re.split still produces sections — chunk_type is doc_section or doc_file
        assert chunks[0]["chunk_type"] in ("doc_section", "doc_file")

    def test_empty_content(self):
        chunks = chunk_markdown("", "test-repo")
        assert chunks == []

    def test_below_min_chunk(self):
        content = "tiny"
        chunks = chunk_markdown(content, "test-repo")
        assert chunks == []

    def test_repo_prefix(self):
        content = (
            "# Header\n\nSufficient substantive body content here for the chunk to be indexed properly even "
            "after the 120-character orphan-heading filter kicks in; payment webhook retry behaviour is "
            "described in enough detail to survive the new minimum.\n"
        )
        chunks = chunk_markdown(content, "test-repo")
        assert all(c["content"].startswith("[Repo: test-repo]") for c in chunks)

    def test_h3_headers_split(self):
        content = (
            "# Top\n\nTop-level content that is long enough to be a valid chunk with substantive body text "
            "exceeding the 120-character body-only orphan-heading filter threshold that the chunker now "
            "enforces on every section it emits.\n\n"
            "## Section\n\nMiddle section with meaningful content for the test case, describing behavior "
            "in enough detail for the body-only length check to pass after stripping the [Repo: ...] "
            "prefix that gets added at emit time by chunk_markdown.\n\n"
            "### Subsection\n\nSubsection content that also passes the minimum body length check by "
            "including enough substantive text about retry semantics, idempotency keys, and webhook "
            "signature validation that the 120-character threshold is comfortably exceeded.\n"
        )
        chunks = chunk_markdown(content, "test-repo")
        assert len(chunks) >= 3


class TestChunkCode:
    """JS/TS code chunking by function/export boundaries."""

    def test_small_file_single_chunk(self):
        # Content must be >= MIN_CHUNK (50 chars) to produce a chunk
        content = "const processPayment = async (data) => {\n  return { status: 'ok', id: data.id };\n};"
        chunks = chunk_code(content, "test-repo", "javascript")
        # Below MAX_CHUNK, above MIN_CHUNK → single chunk
        assert len(chunks) == 1
        assert chunks[0]["chunk_type"] == "code_file"

    def test_empty_content(self):
        chunks = chunk_code("", "test-repo", "javascript")
        assert chunks == []

    def test_below_min_chunk(self):
        content = "x"
        chunks = chunk_code(content, "test-repo", "javascript")
        assert chunks == []

    def test_splits_on_function_boundaries(self):
        # Create content larger than MAX_CHUNK with function boundaries
        func1 = "function processPayment(data) {\n" + "  // processing logic\n" * 100 + "}\n\n"
        func2 = "function handleRefund(data) {\n" + "  // refund logic\n" * 100 + "}\n\n"
        content = func1 + func2
        chunks = chunk_code(content, "test-repo", "javascript", "handler.js")
        assert len(chunks) >= 2
        for c in chunks:
            assert c["content"].startswith("[Repo: test-repo]")

    def test_module_exports_boundary(self):
        content = (
            "const helper = () => {\n" + "  return 'test';\n" * 80 + "};\n\n"
            "module.exports = {\n  helper,\n" + "  // more exports\n" * 50 + "};\n"
        )
        chunks = chunk_code(content, "test-repo", "javascript", "utils.js")
        assert len(chunks) >= 1

    def test_chunk_size_limit(self):
        """No chunk should exceed MAX_CHUNK + reasonable overhead for truncation marker."""
        long_func = "function big() {\n" + "  const x = 'y'.repeat(100);\n" * 200 + "}\n"
        chunks = chunk_code(long_func, "test-repo", "javascript", "big.js")
        for c in chunks:
            # Allow some overhead for the [Repo: ...] prefix and truncation marker
            assert len(c["content"]) <= MAX_CHUNK + 200, f"Chunk too large: {len(c['content'])} chars"

    def test_ts_file_uses_smart_chunking(self):
        """TypeScript files should use the smart JS chunker."""
        content = (
            "export class PaymentHandler {\n" + "  // class body\n" * 80 + "}\n\n"
            "export async function processWebhook(req) {\n" + "  // webhook logic\n" * 80 + "}\n"
        )
        chunks = chunk_code(content, "test-repo", "typescript", "handler.ts")
        assert len(chunks) >= 1
        # Should have code-related chunk types
        types = [c["chunk_type"] for c in chunks]
        assert any("code" in t for t in types)


class TestChunkEdgeCases:
    """Cross-cutting edge cases for all chunkers."""

    def test_single_line_file_proto(self):
        content = "syntax = 'proto3';"
        chunks = chunk_proto(content, "test-repo")
        # 18 chars < MIN_CHUNK (50), should be empty
        assert chunks == []

    def test_single_line_file_markdown(self):
        content = "# Just a title"
        chunks = chunk_markdown(content, "test-repo")
        # 14 chars < MIN_CHUNK, should be empty
        assert chunks == []

    def test_file_exactly_at_min_chunk(self):
        content = "x" * MIN_CHUNK
        chunks = chunk_proto(content, "test-repo")
        assert len(chunks) == 1

    def test_repo_name_in_all_chunks(self):
        """Every chunk from every chunker should contain the repo name."""
        proto_content = "message Test {\n  string id = 1;\n  string name = 2;\n  string value = 3;\n}"
        md_content = "# Test\n\nThis is test content that is long enough to be a valid chunk in the system."
        js_content = "const x = 1;\nconst y = 2;\nexport default { x, y };"

        for chunks in [
            chunk_proto(proto_content, "my-repo"),
            chunk_markdown(md_content, "my-repo"),
            chunk_code(js_content, "my-repo", "javascript"),
        ]:
            for c in chunks:
                assert "my-repo" in c["content"]
