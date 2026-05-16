"""Finalize a /scrape-docs run: validate the provider's docs against the live
public façade and either auto-inject the validator's findings into the
provider's ``index.md`` OR print them and refuse to write.

This is the single mandatory command after /scrape-docs. The architecture
debate verdict (HYBRID, see ``.claude/debug/current/ruling.md``) settled the
behaviour:

    auto-inject ONLY when ALL of:
        1. every spec's verdict ∈ {STRIP_API, KEEP_API, LIVE_AS_AUTHORED, NOT_PUBLIC}
        2. the existing index.md is a "placeholder" (no curator prose)
    otherwise:
        print the report + suggested block to stdout, exit non-zero, require
        a human to paste it. Protects the 30 curator-edited index.md files.

Usage:
    python3 scripts/finalize_scrape.py profiles/pay-com/docs/providers/inpay
    python3 scripts/finalize_scrape.py <dir> --check          # CI: fail on drift
    python3 scripts/finalize_scrape.py <dir> --force-print    # never write
    python3 scripts/finalize_scrape.py <dir> --classify-all   # diagnostic
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_VALIDATOR_PATH = REPO_ROOT / "scripts" / "maint" / "validate_provider_paths.py"


# Late-bound module reference — populated by _load_validator() so tests can
# stub it. Type is intentionally loose; the script imports a few symbols.
def _load_validator():
    spec = importlib.util.spec_from_file_location("validate_provider_paths", _VALIDATOR_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import validator from {_VALIDATOR_PATH}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("validate_provider_paths", mod)
    spec.loader.exec_module(mod)
    return mod


vpp = _load_validator()


# ---------------------------------------------------------------------------
# Constants pinned by the debate ruling.
#
# DO NOT widen DEFINITIVE_VERDICTS without updating the debate record. The
# whole hybrid relies on this set being a strict subset of ALL_KNOWN_VERDICTS;
# unknown verdicts must fail closed (refuse to auto-inject) so a future
# validator change cannot silently change behaviour here.

DEFINITIVE_VERDICTS: frozenset[str] = frozenset(
    {
        vpp.VERDICT_STRIP_API,
        vpp.VERDICT_KEEP_API,
        vpp.VERDICT_LIVE_AS_AUTHORED,
        vpp.VERDICT_NOT_PUBLIC,
    }
)
KNOWN_VERDICTS: frozenset[str] = frozenset(DEFINITIVE_VERDICTS | {vpp.VERDICT_INCONCLUSIVE, vpp.VERDICT_NO_PROBES})

BEGIN_MARKER = "<!-- BEGIN auto-validator -->"
END_MARKER = "<!-- END auto-validator -->"

# Placeholder heuristic — the ruling specifies <50 lines AND no '## ⚠️'
# heading AND no other curator prose markers. We additionally treat any
# index.md that already contains the auto-validator markers as "auto-managed",
# i.e. safe to refresh.
PLACEHOLDER_LINE_LIMIT = 50
CURATOR_PROSE_MARKERS = ("## ⚠️",)


@dataclass
class FinalizeOutcome:
    """Summary of one finalize_scrape.py invocation, useful for both CLI and
    programmatic callers (tests, future MCP tool wiring)."""

    provider_dir: Path
    facade: str | None
    outcomes: list  # list[vpp.SpecProbeOutcome]
    block: str
    all_definitive: bool
    unknown_verdicts: set[str]
    placeholder: bool
    action: str  # one of: 'injected', 'refresh', 'manual', 'check_ok',
    #               'check_drift', 'unknown_verdict', 'no_facade'


# ---------------------------------------------------------------------------
# Pure helpers (network-free, fully unit-testable).


def is_placeholder_index(index_text: str | None, line_limit: int = PLACEHOLDER_LINE_LIMIT) -> bool:
    """Decide if the supplied index.md text is a 'placeholder' that
    finalize_scrape may auto-edit.

    Rules (from ruling.md):
        * missing or empty file → placeholder
        * already has BEGIN/END markers → 'auto-managed', safe to refresh
        * < line_limit lines AND no curator-prose marker → placeholder
        * otherwise → curator-edited (do NOT auto-write)
    """
    if not index_text:
        return True
    if BEGIN_MARKER in index_text and END_MARKER in index_text:
        return True
    if any(marker in index_text for marker in CURATOR_PROSE_MARKERS):
        return False
    line_count = sum(1 for _ in index_text.splitlines())
    return line_count < line_limit


def classify_verdicts(
    outcomes: list,
) -> tuple[bool, set[str]]:
    """Return (all_definitive, unknown_verdicts).

    all_definitive: True iff every outcome's verdict is in DEFINITIVE_VERDICTS
    AND every verdict is in KNOWN_VERDICTS (i.e. validator didn't add a new
    one we don't know how to gate on).

    unknown_verdicts: any verdict the validator emitted that isn't in
    KNOWN_VERDICTS — these MUST cause finalize to refuse to write.
    """
    seen = {o.verdict for o in outcomes}
    unknown = seen - KNOWN_VERDICTS
    if unknown:
        return False, unknown
    return seen.issubset(DEFINITIVE_VERDICTS), unknown


def render_warning_block(facade: str | None, outcomes: list) -> str:
    """Build the markdown block we (may) inject between BEGIN/END markers.

    Output is deterministic so --check can compare for drift exactly.
    """
    lines: list[str] = [BEGIN_MARKER, ""]
    lines.append("## Validator findings (auto-generated)")
    lines.append("")
    facade_str = facade or "(none reachable)"
    lines.append(
        "_Auto-injected by `scripts/finalize_scrape.py`. Probes hit "
        f"`{facade_str}`. **Edit will be overwritten on the next finalize run** — "
        "keep hand-curated notes ABOVE this section._"
    )
    lines.append("")
    if not outcomes:
        lines.append("No specs were probed.")
    else:
        lines.append("| Spec | Sample swagger path | Public path | Verdict | Rationale |")
        lines.append("|------|---------------------|-------------|---------|-----------|")
        for o in outcomes:
            sample_path = o.sample_method_paths[0][1] if o.sample_method_paths else "—"
            if o.verdict == vpp.VERDICT_STRIP_API:
                public = vpp.strip_api_prefix(sample_path)
            elif o.verdict in (
                vpp.VERDICT_KEEP_API,
                vpp.VERDICT_LIVE_AS_AUTHORED,
            ):
                public = sample_path
            elif o.verdict == vpp.VERDICT_NOT_PUBLIC:
                public = "**NOT PUBLIC**"
            else:
                public = "(manual review)"
            rationale = o.rationale.replace("|", "\\|")
            lines.append(f"| `{o.spec_name}` | `{sample_path}` | `{public}` | **{o.verdict}** | {rationale} |")
    lines.append("")
    lines.append(END_MARKER)
    return "\n".join(lines)


def inject_block(index_text: str | None, block: str) -> str:
    """Idempotently insert or replace the marker block. Returns the new text.

    Cases:
        * file missing/empty → block + trailing newline
        * file has BEGIN+END → replace span, leave surrounding bytes intact
        * file has BEGIN but no END → corrupt state, raise (refuse to write)
        * file has neither → append block at the end with one blank line
    """
    text = index_text or ""
    has_begin = BEGIN_MARKER in text
    has_end = END_MARKER in text
    if has_begin and not has_end:
        raise RuntimeError("index.md has BEGIN marker but no END marker — corrupt state, refusing to write")
    if has_begin and has_end:
        before, _, rest = text.partition(BEGIN_MARKER)
        _, _, after = rest.partition(END_MARKER)
        return before + block + after
    if not text:
        return block + "\n"
    if not text.endswith("\n"):
        text += "\n"
    return text + "\n" + block + "\n"


def detect_drift(index_text: str | None, expected_block: str) -> bool:
    """True if the auto-managed span in the file differs from expected.

    Missing markers count as drift (--check should fail loudly so CI catches a
    re-scrape that wasn't run through finalize_scrape).
    """
    if not index_text:
        return True
    if BEGIN_MARKER not in index_text or END_MARKER not in index_text:
        return True

    def _inner(text: str) -> str:
        _, _, rest = text.partition(BEGIN_MARKER)
        inner, _, _ = rest.partition(END_MARKER)
        return inner.strip()

    return _inner(index_text) != _inner(expected_block)


# ---------------------------------------------------------------------------
# Validator runner — thin wrapper around the validator's own orchestration.


def run_validator(
    provider_dir: Path,
    public_host: str | None,
    probe_limit: int,
    timeout: float,
):
    """Run validate_provider_paths against a provider dir.

    Returns the same triple of (specs, dns_results, facade, facade_log,
    outcomes) the validator's main() builds, but exposed as a function so we
    can call it programmatically.
    """
    spec_files = vpp.find_spec_files(provider_dir)
    specs = [s for s in (vpp.parse_spec(p) for p in spec_files) if s is not None]
    hosts = vpp.extract_hosts(specs)
    dns_results = {h: vpp.resolve_dns(h, timeout=timeout) for h in hosts}

    if public_host:
        facade = public_host.rstrip("/")
        facade_log = [
            (
                facade,
                vpp.probe_url(facade + "/", method="GET", timeout=timeout),
            )
        ]
    else:
        base_domain = vpp.detect_base_domain(hosts, dns_results)
        candidates = vpp.generate_facade_candidates(base_domain) if base_domain else []
        facade, facade_log = vpp.pick_facade(candidates, timeout=timeout)

    outcomes: list = []
    if facade:
        for s in specs:
            outcomes.append(vpp.run_spec_probes(s, facade, probe_limit=probe_limit, timeout=timeout))
    return specs, dns_results, facade, facade_log, outcomes


# ---------------------------------------------------------------------------
# Top-level decision logic, exercisable without touching the disk or network.


def decide_action(
    *,
    facade: str | None,
    outcomes: list,
    index_text: str | None,
    force_print: bool,
    check_mode: bool,
) -> tuple[str, str, bool, set[str], bool]:
    """Decide what action finalize should take.

    Returns:
        action: one of 'injected', 'refresh', 'manual', 'check_ok',
                'check_drift', 'unknown_verdict', 'no_facade'
        reason: human-readable rationale string
        all_definitive: bool
        unknown_verdicts: set[str]
        placeholder: bool
    """
    if facade is None:
        return ("no_facade", "no public façade reachable; cannot probe", False, set(), False)

    all_def, unknown = classify_verdicts(outcomes)
    placeholder = is_placeholder_index(index_text)

    expected_block = render_warning_block(facade, outcomes)

    if check_mode:
        if detect_drift(index_text, expected_block):
            return ("check_drift", "auto-validator block missing or stale", all_def, unknown, placeholder)
        return ("check_ok", "auto-validator block in sync", all_def, unknown, placeholder)

    if unknown:
        return (
            "unknown_verdict",
            f"validator emitted unknown verdict(s): {sorted(unknown)} — refuse to write",
            all_def,
            unknown,
            placeholder,
        )

    if force_print:
        return ("manual", "--force-print requested", all_def, unknown, placeholder)

    if not all_def:
        return (
            "manual",
            "at least one verdict is INCONCLUSIVE/NO_PROBES — manual review required",
            all_def,
            unknown,
            placeholder,
        )

    if not placeholder:
        return (
            "manual",
            "index.md is curator-edited (or has CURATOR_PROSE_MARKERS) — refusing to overwrite",
            all_def,
            unknown,
            placeholder,
        )

    if index_text and BEGIN_MARKER in index_text:
        return ("refresh", "auto-managed block exists; refreshing", True, set(), True)
    return ("injected", "first auto-injection", True, set(), True)


# ---------------------------------------------------------------------------
# CLI


def _print_block(label: str, block: str, reason: str) -> None:
    print(f"=== {label} ===", file=sys.stderr)
    print(f"Reason: {reason}", file=sys.stderr)
    print("Paste the following between any pair of BEGIN/END markers in index.md:")
    print()
    print(block)


def _classify_all(providers_root: Path) -> int:
    """Diagnostic: walk every providers/<name>/index.md and report whether
    finalize_scrape would treat it as placeholder. Useful before first live
    rollout to see which dirs the auto-inject path would touch."""
    if not providers_root.is_dir():
        print(f"ERROR: {providers_root} is not a directory", file=sys.stderr)
        return 2
    print(f"# Placeholder classification — {providers_root}\n")
    print("| Provider | Lines | has `## ⚠️` | Markers? | Verdict |")
    print("|----------|-------|------------|----------|---------|")
    placeholder_count = 0
    total = 0
    for sub in sorted(providers_root.iterdir()):
        if not sub.is_dir():
            continue
        idx = sub / "index.md"
        text = idx.read_text(encoding="utf-8") if idx.exists() else ""
        line_count = sum(1 for _ in text.splitlines()) if text else 0
        has_warn = any(m in text for m in CURATOR_PROSE_MARKERS)
        has_markers = BEGIN_MARKER in text and END_MARKER in text
        ph = is_placeholder_index(text)
        if ph:
            placeholder_count += 1
        total += 1
        print(
            f"| {sub.name} | {line_count} | {'yes' if has_warn else 'no'} "
            f"| {'yes' if has_markers else 'no'} | {'placeholder' if ph else 'curator'} |"
        )
    print()
    print(f"_{placeholder_count}/{total} providers classified as placeholder._")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate scraped provider docs and gate auto-injection of validator findings into index.md."
    )
    parser.add_argument("provider_dir", type=Path)
    parser.add_argument(
        "--public-host",
        type=str,
        default=None,
        help="Override façade base URL.",
    )
    parser.add_argument(
        "--probe-limit",
        type=int,
        default=3,
        help="Max GET paths to probe per spec.",
    )
    parser.add_argument("--timeout", type=float, default=6.0, help="Per-request timeout (seconds).")
    parser.add_argument(
        "--check",
        action="store_true",
        help="CI mode: print drift, exit non-zero on diff, never write.",
    )
    parser.add_argument(
        "--force-print",
        action="store_true",
        help="Skip auto-inject even when eligible — always print and exit non-zero.",
    )
    parser.add_argument(
        "--classify-all",
        action="store_true",
        help="Diagnostic: classify every provider dir under <provider_dir>'s parent and exit.",
    )
    args = parser.parse_args(argv)

    if args.classify_all:
        return _classify_all(args.provider_dir)

    if not args.provider_dir.is_dir():
        print(f"ERROR: {args.provider_dir} is not a directory", file=sys.stderr)
        return 2

    print(f"[1/3] Running validator on {args.provider_dir}…", file=sys.stderr)
    _, _, facade, _, outcomes = run_validator(
        args.provider_dir,
        public_host=args.public_host,
        probe_limit=args.probe_limit,
        timeout=args.timeout,
    )

    index_path = args.provider_dir / "index.md"
    index_text = index_path.read_text(encoding="utf-8") if index_path.exists() else None

    block = render_warning_block(facade, outcomes)
    action, reason, all_def, _unknown, placeholder = decide_action(
        facade=facade,
        outcomes=outcomes,
        index_text=index_text,
        force_print=args.force_print,
        check_mode=args.check,
    )

    print(
        f"[2/3] action={action} all_definitive={all_def} placeholder={placeholder}",
        file=sys.stderr,
    )

    if action == "no_facade":
        print(f"ERROR: {reason}", file=sys.stderr)
        return 2
    if action == "unknown_verdict":
        print(f"ERROR: {reason}", file=sys.stderr)
        _print_block("UNKNOWN VERDICT", block, reason)
        return 2
    if action in ("check_ok",):
        print(f"OK: {reason}", file=sys.stderr)
        return 0
    if action == "check_drift":
        print(f"DRIFT: {reason}", file=sys.stderr)
        print(block)
        return 1
    if action == "manual":
        _print_block("MANUAL PASTE REQUIRED", block, reason)
        return 1

    # action in {'injected', 'refresh'}
    new_text = inject_block(index_text, block)
    index_path.write_text(new_text, encoding="utf-8")
    print(
        f"[3/3] {action.upper()} block written to {index_path} ({reason})",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
