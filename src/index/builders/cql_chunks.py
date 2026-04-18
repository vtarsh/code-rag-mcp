"""CQL seeds.cql chunker — extracts provider config rows."""

from __future__ import annotations

import re

from ._common import MAX_CHUNK


def chunk_cql_seeds(content: str, repo_name: str) -> list[dict]:
    """Chunk seeds.cql — each INSERT becomes a separate chunk with provider config metadata.

    This is critical for provider integration: maps provider → payment_method_type, features, currencies.
    """
    chunks = []
    # Pattern to extract provider and payment_method_type from INSERT VALUES
    insert_pattern = re.compile(
        r"INSERT INTO\s+\S+\s*\(([^)]+)\)\s*VALUES\s*\((.+)\)\s*;",
        re.IGNORECASE,
    )

    for line in content.splitlines():
        line = line.strip()
        if not line or not line.upper().startswith("INSERT"):
            continue

        m = insert_pattern.match(line)
        if not m:
            continue

        columns_str = m.group(1)
        columns = [c.strip() for c in columns_str.split(",")]

        # Parse values (handle nested structures like [] and {})
        values_str = m.group(2)
        values = _parse_cql_values(values_str)

        if len(values) < len(columns):
            # Fallback: index whole line as chunk
            chunks.append(
                {
                    "content": f"[Repo: {repo_name}] [Provider Config] {line}",
                    "chunk_type": "provider_config",
                }
            )
            continue

        col_val = dict(zip(columns, values, strict=False))
        provider = col_val.get("provider", "").strip("'\"")
        pmt = col_val.get("payment_method_type", "").strip("'\"")

        # Extract ALL feature flags with explicit true/false values
        feature_cols = [
            "authorization",
            "sale",
            "capture_multiple",
            "capture_partial",
            "refund_multiple",
            "refund_partial",
            "cancel_multiple",
            "cancel_partial",
            "incremental_authorization",
            "payout",
            "verification",
            "network_tokens",
            "external_settlement",
            "internal_settlement",
        ]
        enabled = [c for c in feature_cols if col_val.get(c, "").strip() == "true"]
        disabled = [c for c in feature_cols if col_val.get(c, "").strip() == "false"]

        currencies = col_val.get("processing_currency_codes", "[]")
        settlement_currencies = col_val.get("settlement_currency_codes", "[]")
        precision = col_val.get("default_precision", "").strip("'\"")

        # Card scheme flags
        card_schemes = []
        for scheme in ["visa", "mastercard", "amex", "discover"]:
            val = col_val.get(scheme, "").strip()
            if val == "true":
                card_schemes.append(scheme)

        # Build rich chunk content with ALL values explicit
        header = f"Provider: {provider} | payment_method_type: {pmt}"
        enabled_line = f"Enabled features: {', '.join(enabled)}" if enabled else "Enabled features: none"
        disabled_line = f"Disabled features: {', '.join(disabled)}" if disabled else "Disabled features: none"
        currency_line = f"Processing currencies: {currencies}"
        settlement_line = f"Settlement currencies: {settlement_currencies}"
        precision_line = f"Default precision: {precision}" if precision else ""
        schemes_line = f"Card schemes: {', '.join(card_schemes)}" if card_schemes else ""

        # Explicit boolean matrix for search (key for benchmark accuracy)
        bool_lines = []
        for col in feature_cols:
            val = col_val.get(col, "").strip()
            if val in ("true", "false"):
                bool_lines.append(f"  {col} = {val}")
        bool_matrix = "Feature flags:\n" + "\n".join(bool_lines) if bool_lines else ""

        parts = [
            f"[Repo: {repo_name}] [Provider Config — Source of Truth]",
            header,
            enabled_line,
            disabled_line,
            currency_line,
            settlement_line,
        ]
        if precision_line:
            parts.append(precision_line)
        if schemes_line:
            parts.append(schemes_line)
        if bool_matrix:
            parts.append(bool_matrix)
        parts.append(f"Raw: {line[: MAX_CHUNK - 800]}")

        chunk_content = "\n".join(parts)

        chunks.append(
            {
                "content": chunk_content,
                "chunk_type": "provider_config",
            }
        )

    return chunks


def _parse_cql_values(values_str: str) -> list[str]:
    """Parse CQL VALUES clause, handling nested [] and {} structures."""
    values: list[str] = []
    current = ""
    depth = 0

    for char in values_str:
        if char in ("[", "{"):
            depth += 1
            current += char
        elif char in ("]", "}"):
            depth -= 1
            current += char
        elif char == "," and depth == 0:
            values.append(current.strip())
            current = ""
        elif char == "'" and depth == 0:
            current += char
        else:
            current += char

    if current.strip():
        values.append(current.strip())

    return values
