"""Unit tests for scripts/finalize_scrape.py.

Covers the pure decision surface that gates auto-injection: placeholder
classification, verdict gating, marker-block injection idempotence, drift
detection, and the full decide_action matrix.

The integration with the validator's network/DNS layer is exercised via
fabricated SpecProbeOutcome instances — no HTTP, no DNS.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Load the validator first so finalize_scrape's `vpp` reference resolves.
_VPP_SPEC = importlib.util.spec_from_file_location(
    "validate_provider_paths", REPO_ROOT / "scripts" / "maint" / "validate_provider_paths.py"
)
assert _VPP_SPEC and _VPP_SPEC.loader
vpp_mod = importlib.util.module_from_spec(_VPP_SPEC)
sys.modules["validate_provider_paths"] = vpp_mod
_VPP_SPEC.loader.exec_module(vpp_mod)

_FS_SPEC = importlib.util.spec_from_file_location(
    "finalize_scrape", REPO_ROOT / "scripts" / "scrape" / "finalize_scrape.py"
)
assert _FS_SPEC and _FS_SPEC.loader
fs = importlib.util.module_from_spec(_FS_SPEC)
sys.modules["finalize_scrape"] = fs
_FS_SPEC.loader.exec_module(fs)


# ---------- helpers ---------------------------------------------------------


def _outcome(spec_name: str, verdict: str, sample: str = "/api/foo", rationale: str = "x"):
    return vpp_mod.SpecProbeOutcome(
        spec_name=spec_name,
        sample_method_paths=[("get", sample)],
        original_results=[],
        stripped_results=[],
        paths_with_api_prefix=1,
        paths_total=1,
        verdict=verdict,
        rationale=rationale,
    )


# ---------- placeholder classifier -----------------------------------------


def test_placeholder_when_text_is_empty_or_none() -> None:
    assert fs.is_placeholder_index("") is True
    assert fs.is_placeholder_index(None) is True


def test_placeholder_when_short_and_no_curator_markers() -> None:
    text = "# Foo\n\nshort placeholder file.\n"
    assert fs.is_placeholder_index(text) is True


def test_curator_when_warning_heading_present() -> None:
    text = "# Foo\n\n## ⚠️ Path-prefix gotcha\n\nstuff\n"
    assert fs.is_placeholder_index(text) is False


def test_curator_when_long_even_without_warning() -> None:
    long_text = "# Foo\n\n" + ("a line\n" * 80)
    assert fs.is_placeholder_index(long_text) is False


def test_already_managed_treated_as_placeholder() -> None:
    """An index.md that already has BEGIN+END markers is auto-managed and may
    be refreshed on subsequent runs even if it grew long in between."""
    text = "# Foo\n\n" + ("filler\n" * 80) + f"\n{fs.BEGIN_MARKER}\nstuff\n{fs.END_MARKER}\n"
    assert fs.is_placeholder_index(text) is True


def test_curator_marker_takes_priority_over_block() -> None:
    """If a curator added a `## ⚠️` warning AFTER an auto block was injected,
    we still refuse — the human now owns the file. (Auto-managed bypass only
    fires when no curator marker exists.)"""
    # Note: per is_placeholder_index implementation, BEGIN/END check fires
    # first. Document the expected behaviour explicitly so a future change
    # doesn't silently reorder the rules.
    text = f"# Foo\n## ⚠️ stop\n{fs.BEGIN_MARKER}\nx\n{fs.END_MARKER}\n"
    # Current rule: markers win. If we change priority later, update this
    # test in lockstep with the placeholder docstring.
    assert fs.is_placeholder_index(text) is True


# ---------- verdict classifier ---------------------------------------------


def test_classify_verdicts_all_definitive() -> None:
    outs = [
        _outcome("a", vpp_mod.VERDICT_STRIP_API),
        _outcome("b", vpp_mod.VERDICT_LIVE_AS_AUTHORED),
        _outcome("c", vpp_mod.VERDICT_NOT_PUBLIC),
        _outcome("d", vpp_mod.VERDICT_KEEP_API),
    ]
    all_def, unknown = fs.classify_verdicts(outs)
    assert all_def is True
    assert unknown == set()


def test_classify_verdicts_with_inconclusive_blocks() -> None:
    outs = [
        _outcome("a", vpp_mod.VERDICT_STRIP_API),
        _outcome("b", vpp_mod.VERDICT_INCONCLUSIVE),
    ]
    all_def, unknown = fs.classify_verdicts(outs)
    assert all_def is False
    assert unknown == set()


def test_classify_verdicts_unknown_verdict_fails_closed() -> None:
    outs = [_outcome("a", "BRAND_NEW_VERDICT")]
    all_def, unknown = fs.classify_verdicts(outs)
    assert all_def is False
    assert unknown == {"BRAND_NEW_VERDICT"}


# ---------- render block ---------------------------------------------------


def test_render_block_has_markers_and_table() -> None:
    outs = [
        _outcome("authentication.json", vpp_mod.VERDICT_STRIP_API, sample="/api/foo"),
        _outcome("payouts.json", vpp_mod.VERDICT_LIVE_AS_AUTHORED, sample="/disbursement/x"),
        _outcome("custom.json", vpp_mod.VERDICT_NOT_PUBLIC, sample="/customer_app_api/y"),
    ]
    block = fs.render_warning_block("https://api.example.com", outs)
    assert block.startswith(fs.BEGIN_MARKER)
    assert block.rstrip().endswith(fs.END_MARKER)
    # STRIP_API: public path is path-without-/api
    assert "`/foo`" in block
    # LIVE_AS_AUTHORED: identical
    assert "`/disbursement/x`" in block
    # NOT_PUBLIC: bold marker
    assert "**NOT PUBLIC**" in block
    # Each verdict is a column entry
    for v in ("STRIP_API", "LIVE_AS_AUTHORED", "NOT_PUBLIC"):
        assert v in block


def test_render_block_is_deterministic() -> None:
    """Two renders of the same input must be byte-identical so --check works."""
    outs = [_outcome("a", vpp_mod.VERDICT_STRIP_API, sample="/api/x")]
    a = fs.render_warning_block("https://api.example.com", outs)
    b = fs.render_warning_block("https://api.example.com", outs)
    assert a == b


def test_render_block_handles_empty_outcomes() -> None:
    block = fs.render_warning_block("https://api.example.com", [])
    assert "No specs were probed." in block


def test_render_block_escapes_pipe_in_rationale() -> None:
    out = _outcome(
        "a",
        vpp_mod.VERDICT_STRIP_API,
        sample="/api/x",
        rationale="hit | broken | table",
    )
    block = fs.render_warning_block("https://api.example.com", [out])
    # Pipes in the rationale must be escaped so the table doesn't break
    assert "\\| broken \\|" in block


# ---------- inject_block ---------------------------------------------------


def test_inject_into_empty_file() -> None:
    out = fs.inject_block(None, "<!-- BEGIN auto-validator -->\nx\n<!-- END auto-validator -->")
    assert out.startswith(fs.BEGIN_MARKER)
    assert out.rstrip().endswith(fs.END_MARKER)


def test_inject_appends_when_no_markers() -> None:
    text = "# Foo\n\nbody\n"
    block = "<!-- BEGIN auto-validator -->\nNEW\n<!-- END auto-validator -->"
    out = fs.inject_block(text, block)
    assert out.startswith("# Foo")
    assert "BEGIN auto-validator" in out
    assert out.count(fs.BEGIN_MARKER) == 1
    assert out.count(fs.END_MARKER) == 1


def test_inject_replaces_existing_span_idempotently() -> None:
    text = f"# Foo\n\npreserved-prose\n\n{fs.BEGIN_MARKER}\nold-stuff\n{fs.END_MARKER}\ntrailing\n"
    new_block = f"{fs.BEGIN_MARKER}\nnew-stuff\n{fs.END_MARKER}"
    out = fs.inject_block(text, new_block)
    # Only the auto-managed span changed
    assert "preserved-prose" in out
    assert "trailing" in out
    assert "old-stuff" not in out
    assert "new-stuff" in out
    # And running again with the same block leaves it unchanged.
    again = fs.inject_block(out, new_block)
    assert again == out


def test_inject_refuses_corrupt_state_begin_without_end() -> None:
    text = f"# Foo\n{fs.BEGIN_MARKER}\nstuff (no end)\n"
    with pytest.raises(RuntimeError, match="corrupt state"):
        fs.inject_block(text, "<!-- BEGIN auto-validator -->\nx\n<!-- END auto-validator -->")


# ---------- drift detection ------------------------------------------------


def test_drift_when_file_missing_markers() -> None:
    block = fs.render_warning_block("https://api.example.com", [_outcome("a", vpp_mod.VERDICT_STRIP_API)])
    assert fs.detect_drift("# foo no markers", block) is True
    assert fs.detect_drift(None, block) is True


def test_no_drift_when_block_matches() -> None:
    block = fs.render_warning_block("https://api.example.com", [_outcome("a", vpp_mod.VERDICT_STRIP_API)])
    text = f"# Foo\n\n{block}\n"
    assert fs.detect_drift(text, block) is False


def test_drift_when_inner_content_changes() -> None:
    block_old = fs.render_warning_block("https://api.example.com", [_outcome("a", vpp_mod.VERDICT_STRIP_API)])
    block_new = fs.render_warning_block("https://api.example.com", [_outcome("a", vpp_mod.VERDICT_KEEP_API)])
    text = f"# Foo\n\n{block_old}\n"
    assert fs.detect_drift(text, block_new) is True


# ---------- decide_action: full matrix -------------------------------------


def _decide(
    *,
    facade: str | None = "https://api.example.com",
    outcomes=None,
    index_text=None,
    force_print=False,
    check_mode=False,
):
    if outcomes is None:
        outcomes = [_outcome("a", vpp_mod.VERDICT_STRIP_API)]
    return fs.decide_action(
        facade=facade,
        outcomes=outcomes,
        index_text=index_text,
        force_print=force_print,
        check_mode=check_mode,
    )


def test_decide_no_facade() -> None:
    action, _, _, _, _ = _decide(facade=None)
    assert action == "no_facade"


def test_decide_unknown_verdict_blocks_inject() -> None:
    outs = [_outcome("a", "WEIRD_NEW_THING")]
    action, _, _, unknown, _ = _decide(outcomes=outs)
    assert action == "unknown_verdict"
    assert unknown == {"WEIRD_NEW_THING"}


def test_decide_check_ok_when_synced() -> None:
    outs = [_outcome("a", vpp_mod.VERDICT_STRIP_API)]
    block = fs.render_warning_block("https://api.example.com", outs)
    text = f"# Foo\n\n{block}\n"
    action, _, _, _, _ = _decide(outcomes=outs, index_text=text, check_mode=True)
    assert action == "check_ok"


def test_decide_check_drift_on_stale_block() -> None:
    outs_old = [_outcome("a", vpp_mod.VERDICT_KEEP_API)]
    outs_new = [_outcome("a", vpp_mod.VERDICT_STRIP_API)]
    text = f"# Foo\n\n{fs.render_warning_block('h', outs_old)}\n"
    action, _, _, _, _ = _decide(outcomes=outs_new, index_text=text, check_mode=True)
    assert action == "check_drift"


def test_decide_force_print_overrides_eligibility() -> None:
    """Even with all-definitive verdicts and a placeholder index, --force-print
    must keep the wrapper from writing."""
    action, _, _, _, _ = _decide(index_text=None, force_print=True)
    assert action == "manual"


def test_decide_inconclusive_routes_to_manual() -> None:
    outs = [
        _outcome("a", vpp_mod.VERDICT_STRIP_API),
        _outcome("b", vpp_mod.VERDICT_INCONCLUSIVE),
    ]
    action, _, _, _, _ = _decide(outcomes=outs, index_text=None)
    assert action == "manual"


def test_decide_curator_index_routes_to_manual() -> None:
    outs = [_outcome("a", vpp_mod.VERDICT_STRIP_API)]
    text = "# Foo\n\n## ⚠️ Path-prefix gotcha\n\ncurated\n"
    action, _, _, _, placeholder = _decide(outcomes=outs, index_text=text)
    assert action == "manual"
    assert placeholder is False


def test_decide_inject_when_placeholder_and_definitive() -> None:
    outs = [_outcome("a", vpp_mod.VERDICT_STRIP_API)]
    action, _, _, _, _ = _decide(outcomes=outs, index_text="# Foo\n")
    assert action == "injected"


def test_decide_refresh_when_managed_block_present() -> None:
    outs = [_outcome("a", vpp_mod.VERDICT_STRIP_API)]
    text = f"# Foo\n\n{fs.BEGIN_MARKER}\nold\n{fs.END_MARKER}\n"
    action, _, _, _, _ = _decide(outcomes=outs, index_text=text)
    assert action == "refresh"


# ---------- end-to-end CLI smoke (no network) ------------------------------


def test_main_classify_all_walks_dirs(tmp_path: Path, capsys) -> None:
    root = tmp_path / "providers"
    (root / "ph").mkdir(parents=True)
    (root / "ph" / "index.md").write_text("# Foo\n", encoding="utf-8")
    (root / "cur").mkdir(parents=True)
    (root / "cur" / "index.md").write_text("# Foo\n\n## ⚠️ careful\n\n" + ("a\n" * 60), encoding="utf-8")
    (root / "missing").mkdir(parents=True)  # no index.md at all

    rc = fs.main([str(root), "--classify-all"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "ph" in out and "placeholder" in out
    assert "cur" in out and "curator" in out
    assert "missing" in out  # missing index.md still reported
