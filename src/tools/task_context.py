"""Code-anchored signal extraction from JIRA task body for retrieval enrichment.

Opaque/symptom JIRA titles ("Prod bug UNKNOWN", "Refactoring API Logs Flow")
carry no code signal. The body usually does — identifiers, error strings,
file paths. Step 2 of the recall-fix plan uses this module to extract ONLY
code-anchored substance and run a SEPARATE retrieval pass on it (then RRF-
merge with the title pass). The naive concat path (A3, 2026-05-20) lost 18pp
on n=665 because prose drowned the title.

Pure functions, no I/O. Env-gating happens at the caller (hybrid_search).
"""

from __future__ import annotations

import re

# --- Sanitization ----------------------------------------------------------

# URLs (incl. credentials embedded in query string)
_URL_RE = re.compile(r"https?://[^\s)>\]]+|www\.[^\s)>\]]+", re.IGNORECASE)

# JWTs (eyJ-prefixed three-segment base64url)
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\b")

# Hex strings ≥32 chars — likely hashes / keys / tokens
_HEX_HASH_RE = re.compile(r"\b[a-fA-F0-9]{32,}\b")

# k=v / k: v pairs whose key looks credential-shaped
_AUTH_KV_RE = re.compile(
    r"\b(?:password|passwd|pwd|secret|api[_-]?key|access[_-]?token|"
    r"bearer|authorization|x-api-key)\s*[:=]\s*\S+",
    re.IGNORECASE,
)

# Markdown inline code: keep content, strip backticks.
_MD_INLINE_CODE_RE = re.compile(r"`+([^`\n]+)`+")
# Markdown fenced code: keep content, strip ``` fences and language tag.
_MD_FENCE_RE = re.compile(r"```[a-zA-Z0-9_-]*\n?(.*?)```", re.DOTALL)

# FTS5 syntax-breakers (same set as _sanitize_fts_input + `=` for k=v residue).
# `/` is handled separately (path separator → space).
_FTS_BREAKERS_RE = re.compile(r"""[*":,()\[\]`'\\=]""")


def sanitize_body(body: str) -> str:
    """Strip credentials, URLs, FTS5-breaking chars, markdown formatting.

    Returns plain-text safe to pass through `_sanitize_fts_input` /
    `sanitize_fts_query` downstream. Idempotent.
    """
    if not body:
        return ""
    s = body
    # Unwrap fenced code first so URLs/JWTs inside code blocks still get scrubbed.
    s = _MD_FENCE_RE.sub(lambda m: m.group(1), s)
    s = _MD_INLINE_CODE_RE.sub(lambda m: m.group(1), s)
    # Credentials / opaque blobs.
    s = _URL_RE.sub(" ", s)
    s = _JWT_RE.sub(" ", s)
    s = _AUTH_KV_RE.sub(" ", s)
    s = _HEX_HASH_RE.sub(" ", s)
    # FTS5-breakers + path separator.
    s = _FTS_BREAKERS_RE.sub(" ", s)
    s = s.replace("/", " ")
    # Collapse whitespace.
    return re.sub(r"\s+", " ", s).strip()


# --- Code-anchored extraction ---------------------------------------------

# Compound identifiers — same families as bench_steps_to_find but lowered
# length threshold (≥6 vs 8). Body sentences are shorter than file snippets;
# the 8-char bench cutoff was tuned to drop noise from a HUGE token stream,
# whereas body extraction starts from a much smaller pool already filtered
# by code-anchored regexes.
_COMPOUND_PASCAL_RE = re.compile(r"\b(?:[A-Z][a-z0-9]+){2,}\b")
_COMPOUND_CAMEL_RE = re.compile(r"\b[a-z][a-z0-9]*(?:[A-Z][a-z0-9]+){1,}\b")
_COMPOUND_SNAKE_RE = re.compile(r"\b[a-z][a-z0-9]+(?:_[a-z0-9]+){1,}\b")

# Hyphenated lowercase ≥2 parts — `request-logs`, `api-keys`, `clean-external-trace-headers`.
_HYPHENATED_RE = re.compile(r"\b[a-z][a-z0-9]*(?:-[a-z][a-z0-9]+){1,}\b")

# File-with-extension references — `MerchantPage.tsx`, `schemas.proto`, `consts.ts`.
_FILE_PATH_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9_\-]*\.(?:tsx|ts|jsx|js|proto|py|go|yaml|yml|json|sql|md|sh|rs)\b")

# ALL_CAPS abbreviations (3-10 chars).
_ABBREV_RE = re.compile(r"\b[A-Z]{3,10}\b")
_ABBREV_STOPWORDS = frozenset(
    {
        # Generic technical noise — already implied or non-discriminating.
        "API",
        "URL",
        "URI",
        "HTTP",
        "HTTPS",
        "JSON",
        "YAML",
        "XML",
        "HTML",
        "CSS",
        "PDF",
        "CSV",
        "ZIP",
        # Status / log noise.
        "TODO",
        "FIXME",
        "NOTE",
        "WIP",
        "BUG",
        "INFO",
        "WARN",
        "ERROR",
        "DEBUG",
        "TRACE",
        # English ALL_CAPS quirks.
        "AND",
        "FOR",
        "THE",
        "NOT",
        "USE",
        "GET",
        "SET",
        # Common HTTP methods.
        "POST",
        "PUT",
        "PATCH",
    }
)

# Words present in compound forms shouldn't ALSO appear as plain tokens.
_HYPHEN_STOPWORDS = frozenset(
    {
        "low-level",
        "high-level",
        "long-term",
        "short-term",
        "real-time",
        "out-of",
        "follow-up",
        "drop-down",
    }
)


def extract_code_anchored(text: str, *, k: int = 12) -> list[str]:
    """Extract up to k code-anchored tokens from arbitrary text.

    Selection: compound identifiers (PascalCase / camelCase / snake_case ≥6),
    hyphenated multi-part names (≥6 chars), file-with-extension references,
    and ALL_CAPS abbreviations (excluding generic stopwords).

    Tokens are ranked by (weight × occurrence, length, alpha for determinism).
    Weights reflect how discriminating each pattern is on average:
      - file path: 4
      - compound identifier: 3
      - hyphenated: 2
      - abbreviation: 1
    """
    if not text:
        return []
    counts: dict[str, int] = {}

    def _add(tok: str, weight: int) -> None:
        if not tok or len(tok) < 6:
            return
        counts[tok] = counts.get(tok, 0) + weight

    # File-with-extension first (highest signal).
    for m in _FILE_PATH_RE.finditer(text):
        _add(m.group(0), 4)
    # Compound identifiers.
    for rx in (_COMPOUND_PASCAL_RE, _COMPOUND_CAMEL_RE, _COMPOUND_SNAKE_RE):
        for m in rx.finditer(text):
            _add(m.group(0), 3)
    # Hyphenated lowercase compounds.
    for m in _HYPHENATED_RE.finditer(text):
        tok = m.group(0)
        if tok.lower() in _HYPHEN_STOPWORDS:
            continue
        _add(tok, 2)
    # ALL_CAPS abbreviations.
    for m in _ABBREV_RE.finditer(text):
        tok = m.group(0)
        if tok in _ABBREV_STOPWORDS or len(tok) < 3:
            continue
        # weight 1, no min-length 6 requirement — abbreviations are short by nature.
        counts[tok] = counts.get(tok, 0) + 1

    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], -len(kv[0]), kv[0]))
    return [tok for tok, _ in ranked[:k]]


# --- Public API ------------------------------------------------------------

# Split body tokens into word-parts for title-overlap filtering. The previous
# substring check missed compound shapes — title "payment method options" with
# body `payment_method_options` slipped through and dragged a noisy
# `three_ds_*` token cluster into the FTS pool, displacing title's correct
# top-3 (n=30 CORE-opaque cluster 2026-05-21).
_WORD_SPLIT_RE = re.compile(r"[\s_\-]+|(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def _word_parts(s: str) -> frozenset[str]:
    """Lowercase word-parts of `s` (camelCase / snake_case / kebab / spaces split)."""
    if not s:
        return frozenset()
    return frozenset(p.lower() for p in _WORD_SPLIT_RE.split(s) if len(p) >= 3)


def build_body_query(body: str, title: str = "") -> str | None:
    """Sanitize body + extract code-anchored tokens → FTS-ready space-joined query.

    Returns None when the body has no discriminating code signal (<2 novel
    tokens after title overlap). A body token is "novel" if its word-parts
    are NOT a subset of the title's word-parts — this catches compound
    rephrasings like body `payment_method_options` vs title
    "payment method options" that a substring check would let through.
    """
    if not body:
        return None
    clean = sanitize_body(body)
    tokens = extract_code_anchored(clean, k=12)
    if len(tokens) < 2:
        return None
    title_parts = _word_parts(title or "")
    novel: list[str] = []
    for tok in tokens:
        tok_parts = _word_parts(tok)
        if not tok_parts:
            continue
        # Drop if every word-part is already in the title (no new signal).
        if tok_parts.issubset(title_parts):
            continue
        novel.append(tok)
    if len(novel) < 2:
        return None
    return " ".join(novel)
