"""Tests for the chunker audit fixes in src/index/builders/docs_chunks.py.

Covers three audit findings from 2026-04-22:
1. MAX_CHUNK enforcement via _subsplit_oversized (oversized sections get
   paragraph-boundary splits with overlap).
2. content_hash dedup that ignores the [Repo: X] / [... Docs: X] prefix.
3. Orphan-heading filter: MIN_DOC_BODY applied to body-only length.
"""

from __future__ import annotations

import math

from src.index.builders._common import MAX_CHUNK
from src.index.builders.docs_chunks import (
    MIN_DOC_BODY,
    _subsplit_oversized,
    chunk_markdown,
    content_hash,
)


class TestSubsplitOversized:
    """_subsplit_oversized: MAX_CHUNK enforcement for oversized sections."""

    def test_subsplit_oversized_long_section(self):
        # Build a 12000-char section of 40 paragraphs, each ~300 chars long.
        para = ("word " * 60).strip()  # ~299 chars
        section = "\n\n".join([para] * 40)
        assert len(section) > 11000
        chunks = _subsplit_oversized(section, max_chars=MAX_CHUNK, overlap=400)
        # Should split into ~3-4 chunks
        assert 3 <= len(chunks) <= 6
        for c in chunks:
            assert len(c) <= MAX_CHUNK, f"Chunk exceeds MAX_CHUNK: {len(c)}"
        # Overlap: successive chunks share their tail/head up to ~400 chars.
        # Verify by checking the last `overlap` chars of chunk[i] appear in chunk[i+1].
        for i in range(len(chunks) - 1):
            tail = chunks[i][-400:]
            # At least one paragraph from tail should appear in next chunk.
            # Find a non-empty "word " paragraph and check membership.
            tail_paras = [p for p in tail.split("\n\n") if p.strip()]
            assert any(p in chunks[i + 1] for p in tail_paras), "No overlap between successive chunks"

    def test_subsplit_preserves_small(self):
        section = "x" * 2000
        chunks = _subsplit_oversized(section, max_chars=MAX_CHUNK)
        assert chunks == [section]

    def test_subsplit_hard_split_single_paragraph(self):
        # 10000-char single paragraph (no \n\n) must be hard-split.
        section = "a" * 10000
        chunks = _subsplit_oversized(section, max_chars=MAX_CHUNK, overlap=400)
        # step = max_chars - overlap = 3600; ceil(10000 / 3600) = 3
        expected = math.ceil(10000 / (MAX_CHUNK - 400))
        assert len(chunks) == expected, f"Expected {expected} chunks, got {len(chunks)}"
        for c in chunks:
            assert len(c) <= MAX_CHUNK


class TestContentHash:
    """content_hash: prefix-independent body hashing for dedup."""

    def test_content_hash_ignores_prefix(self):
        body = (
            "## Responses\n\nReturns 200 OK when the webhook signature validates "
            "against the shared secret configured for the provider."
        )
        a = f"[Repo: alpha] {body}"
        b = f"[Repo: beta] {body}"
        assert content_hash(a) == content_hash(b)

    def test_content_hash_ignores_provider_docs_prefix(self):
        body = (
            "### Webhook retries\n\nThe platform retries failed webhook deliveries "
            "with exponential backoff for up to 24 hours before giving up entirely."
        )
        a = f"[Plaid Docs: webhooks] {body}"
        b = f"[Credorax Docs: webhooks] {body}"
        assert content_hash(a) == content_hash(b)

    def test_content_hash_different_bodies_differ(self):
        a = "[Repo: x] first body that is long enough to be meaningful content for hashing purposes"
        b = "[Repo: x] second body that is long enough to be meaningful content for hashing purposes"
        assert content_hash(a) != content_hash(b)

    def test_content_hash_length(self):
        h = content_hash("[Repo: x] " + "y" * 500)
        assert len(h) == 16
        assert all(c in "0123456789abcdef" for c in h)


class TestOrphanHeadingFilter:
    """MIN_DOC_BODY filter: drops orphan headings / TODO markers."""

    def test_orphan_heading_filter(self):
        # "## Some Heading\n\nSee [other doc]" — body ~32 chars, way under 120.
        content = "## Some Heading\n\nSee [other doc]"
        chunks = chunk_markdown(content, "test-repo")
        assert chunks == []

    def test_orphan_heading_with_link_only_dropped(self):
        content = "### TODO\n\nSee also: [link](https://example.com)"
        chunks = chunk_markdown(content, "test-repo")
        assert chunks == []

    def test_substantive_body_kept(self):
        # ~400-char real content
        body = (
            "This section describes the webhook retry policy in detail: the service "
            "retries failed deliveries using exponential backoff, starting at one second "
            "and doubling up to a cap of five minutes, for a maximum duration of 24 "
            "hours before moving the delivery to the dead-letter queue for manual review."
        )
        assert len(body) > MIN_DOC_BODY
        content = f"## Webhook Retries\n\n{body}"
        chunks = chunk_markdown(content, "test-repo")
        assert len(chunks) == 1
        assert "[Repo: test-repo]" in chunks[0]["content"]


class TestChunkMarkdownIntegration:
    """chunk_markdown integrates _subsplit_oversized for oversized sections."""

    def test_oversized_section_subsplit_into_multiple_chunks(self):
        # Build a section whose body (after header+prefix) exceeds MAX_CHUNK.
        para = ("word " * 60).strip()
        big_body = "\n\n".join([para] * 40)
        content = f"## Big Section\n\n{big_body}"
        chunks = chunk_markdown(content, "test-repo")
        # Should produce multiple chunks, each <= MAX_CHUNK + prefix overhead.
        assert len(chunks) >= 2
        for c in chunks:
            # Prefix "[Repo: test-repo] " is ~18 chars; allow small overhead.
            assert len(c["content"]) <= MAX_CHUNK + 50
