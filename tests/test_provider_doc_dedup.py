"""Per-provider chunk deduplication for provider_doc file_type.

Verifies that identical boilerplate (nav links, copyright, method indexes)
within ONE vendor's docs collapses to a single indexed chunk, while identical
content across DIFFERENT vendors stays separate (vendor-specific context).
"""

from __future__ import annotations

import sqlite3

from src.index.builders import docs_indexer
from src.index.builders.db import create_db

_BODY_A = "## Authentication\n\n" + (
    "Use bearer tokens in the Authorization header. The token is a 32-character hex string. " * 6
)
_BODY_B = "## Payments\n\n" + (
    "Process card payments with idempotency keys for safe retries. Returns transaction id on success. " * 6
)


def _setup(tmp_path, monkeypatch) -> sqlite3.Connection:
    providers_dir = tmp_path / "providers"
    providers_dir.mkdir()

    stripe = providers_dir / "stripe"
    stripe.mkdir()
    (stripe / "auth.md").write_text(_BODY_A)
    (stripe / "webhooks.md").write_text(_BODY_A)  # duplicate of auth.md
    (stripe / "payments.md").write_text(_BODY_B)

    paypal = providers_dir / "paypal"
    paypal.mkdir()
    (paypal / "auth.md").write_text(_BODY_A)  # same body as stripe/auth.md, different vendor

    monkeypatch.setattr(docs_indexer, "PROVIDERS_DIR", providers_dir, raising=True)

    conn = sqlite3.connect(str(tmp_path / "knowledge.db"))
    create_db(conn)
    return conn


def test_same_provider_identical_body_dedups(tmp_path, monkeypatch):
    conn = _setup(tmp_path, monkeypatch)
    files, _ = docs_indexer.index_providers(conn)
    assert files == 4
    stripe_chunks = conn.execute("SELECT COUNT(*) FROM chunks WHERE repo_name = 'stripe-docs'").fetchone()[0]
    # auth.md and webhooks.md collapse to 1; payments.md stays separate → 2
    assert stripe_chunks == 2


def test_different_providers_identical_body_kept_separate(tmp_path, monkeypatch):
    conn = _setup(tmp_path, monkeypatch)
    docs_indexer.index_providers(conn)
    paypal_chunks = conn.execute("SELECT COUNT(*) FROM chunks WHERE repo_name = 'paypal-docs'").fetchone()[0]
    # paypal/auth.md has same body as stripe/auth.md but different provider
    assert paypal_chunks == 1


def test_different_bodies_same_provider_both_indexed(tmp_path, monkeypatch):
    conn = _setup(tmp_path, monkeypatch)
    docs_indexer.index_providers(conn)
    rows = conn.execute("SELECT content FROM chunks WHERE repo_name = 'stripe-docs'").fetchall()
    bodies = "\n".join(r[0] for r in rows)
    assert "Authentication" in bodies
    assert "Payments" in bodies


def test_idempotent_reindex(tmp_path, monkeypatch):
    conn = _setup(tmp_path, monkeypatch)
    docs_indexer.index_providers(conn)
    count1 = conn.execute("SELECT COUNT(*) FROM chunks WHERE file_type = 'provider_doc'").fetchone()[0]
    docs_indexer.index_providers(conn)
    count2 = conn.execute("SELECT COUNT(*) FROM chunks WHERE file_type = 'provider_doc'").fetchone()[0]
    assert count1 == count2 == 3  # stripe(2) + paypal(1)


def test_dedup_scoped_to_provider_doc_only(tmp_path, monkeypatch):
    """Other indexers (gotchas, references, etc.) keep their own dedup scope.

    Sanity: provider_doc dedup stays inside index_providers and does not leak
    to a different file_type by sharing a module-level set.
    """
    conn = _setup(tmp_path, monkeypatch)
    docs_indexer.index_providers(conn)
    # All inserted chunks must be provider_doc type
    types = {r[0] for r in conn.execute("SELECT DISTINCT file_type FROM chunks").fetchall()}
    assert types == {"provider_doc"}
