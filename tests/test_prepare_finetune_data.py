"""Unit tests for scripts.prepare_finetune_data helpers.

Currently covers:
    * verify_no_query_leakage — query-level Jaccard leakage guard used to
      cross-check v12 train vs held-out Jira / runtime sets.
"""

from __future__ import annotations

import re

import pytest

from scripts.prepare_finetune_data import (
    _default_query_tokens,
    verify_no_query_leakage,
)

# --------------------------------------------------------------------------- #
#                              trivial pass                                   #
# --------------------------------------------------------------------------- #


def test_verify_no_query_leakage_trivial_pass_strings() -> None:
    """Fully disjoint vocabularies must never raise."""
    train = [
        "alias payment method update",
        "refund webhook retry logic",
    ]
    holdout = [
        "seeds cql provider config",
        "temporal workflow polling timeout",
    ]
    verify_no_query_leakage(train, holdout, threshold=0.5)


def test_verify_no_query_leakage_trivial_pass_dicts() -> None:
    """Dicts with summary/description are concatenated before tokenising."""
    train = [
        {"ticket_id": "BO-1", "summary": "alias payment update", "description": ""},
        {"ticket_id": "BO-2", "summary": "refund webhook retry", "description": ""},
    ]
    holdout = [
        {"ticket_id": "PI-1", "summary": "seeds cql config", "description": ""},
    ]
    verify_no_query_leakage(train, holdout, threshold=0.5)


# --------------------------------------------------------------------------- #
#                              trivial fail                                   #
# --------------------------------------------------------------------------- #


def test_verify_no_query_leakage_trivial_fail_identical() -> None:
    """Identical query must raise with the offending pair in the message."""
    train = ["add doNotExpire flag to trustly provider"]
    holdout = ["add doNotExpire flag to trustly provider"]
    with pytest.raises(ValueError, match="query-leakage"):
        verify_no_query_leakage(train, holdout, threshold=0.5)


def test_verify_no_query_leakage_trivial_fail_has_preview() -> None:
    """The ValueError message must mention threshold and show the pair index."""
    train = ["alias payment method update for trustly"]
    holdout = ["alias payment method update for trustly"]
    with pytest.raises(ValueError) as exc:
        verify_no_query_leakage(train, holdout, threshold=0.5)
    msg = str(exc.value)
    assert "jaccard=1.0" in msg
    assert "train[0]" in msg and "holdout[0]" in msg


# --------------------------------------------------------------------------- #
#                            partial-overlap pass                             #
# --------------------------------------------------------------------------- #


def test_verify_no_query_leakage_partial_overlap_pass() -> None:
    """Some shared tokens but Jaccard < threshold must NOT raise."""
    # 3 shared tokens ("payment","method","update") out of a much larger union.
    # Jaccard ~ 3 / (>10) < 0.5 so pass at threshold=0.5.
    train = ["payment method update for new trustly sepa payout flow backfill"]
    holdout = ["payment method update alias configuration seed cql verification"]
    # Sanity: this is genuinely partial.
    t1 = _default_query_tokens(train[0])
    t2 = _default_query_tokens(holdout[0])
    jac = len(t1 & t2) / len(t1 | t2)
    assert 0.1 < jac < 0.5, f"test assumption broken: jaccard={jac}"

    verify_no_query_leakage(train, holdout, threshold=0.5)


def test_verify_no_query_leakage_tight_threshold_flags_partial() -> None:
    """Same partial-overlap pair fails once we tighten threshold below jac."""
    train = ["payment method update for new trustly sepa payout flow backfill"]
    holdout = ["payment method update alias configuration seed cql verification"]
    with pytest.raises(ValueError):
        verify_no_query_leakage(train, holdout, threshold=0.1)


# --------------------------------------------------------------------------- #
#                        custom token_fn / edge cases                         #
# --------------------------------------------------------------------------- #


def test_verify_no_query_leakage_empty_inputs_pass() -> None:
    """Empty sides are never flagged."""
    verify_no_query_leakage([], ["foo bar baz"], threshold=0.5)
    verify_no_query_leakage(["foo bar baz"], [], threshold=0.5)
    verify_no_query_leakage([""], [""], threshold=0.5)


def test_verify_no_query_leakage_custom_token_fn() -> None:
    """Custom tokenizer can loosen / tighten the signal."""
    # 1-char tokens so "a b c" leaks against "a b c d" despite short text.
    tok = lambda s: set(re.findall(r"\w", (s or "").lower()))  # noqa: E731
    with pytest.raises(ValueError):
        verify_no_query_leakage(["a b c"], ["a b c d"], token_fn=tok, threshold=0.5)
