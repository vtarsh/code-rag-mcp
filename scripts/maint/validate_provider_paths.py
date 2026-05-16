"""Validate scraped provider documentation against the provider's live public façade.

Catches the class of mistakes we hit with Inpay 2026-04-25:

    1. swagger.json declares paths with /api/ prefix that the public edge proxy
       silently strips, so the path-as-authored 302-redirects to the docs site;
    2. swagger.json lists internal back-end hosts (e.g. backend-v2.internal,
       *-backend-v2.<domain>) that do NOT resolve in public DNS, leaving the
       caller to guess the public façade host;
    3. multiple specs in one provider mix conventions — some carry /api/, some
       don't — so a single grep won't surface the mismatch.

The validator probes each spec against a public-façade host (auto-discovered
from the resolvable swagger servers, or supplied via --public-host) and prints
per-spec verdicts: STRIP_API, KEEP_AS_IS, NOT_PUBLIC, INCONCLUSIVE.

Usage:
    python3 scripts/validate_provider_paths.py profiles/pay-com/docs/providers/inpay
    python3 scripts/validate_provider_paths.py <dir> --public-host https://test-api.inpay.com
    python3 scripts/validate_provider_paths.py <dir> --probe-limit 5 --output report.md
"""

from __future__ import annotations

import argparse
import json
import re
import socket
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

# ---------------------------------------------------------------------------
# Status-code semantics. The validator only cares about whether a route exists
# on the public façade; it deliberately does not authenticate, so it expects
# AUTH_REQUIRED / BAD_INPUT for live routes whose schemas we don't satisfy.

STATUS_LIVE = "live"  # 2xx, 401, 403, 400, 422 — route exists on this host
STATUS_NOT_FOUND = "not_found"  # 404 — route absent on this host
STATUS_REDIRECT_DOCS = "redirect_to_docs"  # 3xx → docs/marketing — typical edge "404"
STATUS_REDIRECT_OTHER = "redirect_other"  # 3xx → API-ish location — needs manual look
STATUS_SERVER_ERROR = "server_error"  # 5xx — assume route exists, server complained
STATUS_UNREACHABLE = "unreachable"  # network/DNS/TLS error
STATUS_OTHER = "other"  # everything else (rare)

LIVE_LIKE = {STATUS_LIVE, STATUS_SERVER_ERROR}
DEAD_LIKE = {STATUS_NOT_FOUND, STATUS_REDIRECT_DOCS}

# Verdicts for a single spec's /api/ prefix question
VERDICT_STRIP_API = "STRIP_API"
VERDICT_KEEP_API = "KEEP_API"
VERDICT_NOT_PUBLIC = "NOT_PUBLIC"
VERDICT_LIVE_AS_AUTHORED = "LIVE_AS_AUTHORED"
VERDICT_INCONCLUSIVE = "INCONCLUSIVE"
VERDICT_NO_PROBES = "NO_PROBES"

DOCS_HINTS = ("/docs", "/documentation", "/help", "/getting-started")
PLACEHOLDER_RE = re.compile(r"\{[^/}]+\}")


# ---------------------------------------------------------------------------
# Data classes


@dataclass
class SpecSummary:
    file_name: str
    title: str
    version: str | None
    spec_paths: list[str]  # raw path strings, e.g. /api/foo/{id}
    methods_per_path: dict[str, list[str]]  # path -> ["get", "post", ...]
    servers: list[str]  # full URLs as authored


@dataclass
class HostDnsStatus:
    host: str
    resolves: bool
    error: str | None = None


@dataclass
class ProbeResult:
    url: str
    method: str
    status_code: int | None
    location: str | None
    semantic: str  # one of STATUS_*
    content_type: str | None = None
    error: str | None = None


@dataclass
class SpecProbeOutcome:
    spec_name: str
    sample_method_paths: list[tuple[str, str]]  # [(method, raw_path), ...]
    original_results: list[ProbeResult]
    stripped_results: list[ProbeResult]  # may be [] if no /api/ prefix
    paths_with_api_prefix: int
    paths_total: int
    verdict: str
    rationale: str


# ---------------------------------------------------------------------------
# Pure-ish helpers (no network, fully unit-testable)


def find_spec_files(provider_dir: Path) -> list[Path]:
    """Collect all OpenAPI / Swagger JSON files in <provider_dir>.

    Looks first in <provider_dir>/swagger/, then falls back to <provider_dir>
    itself for providers that store specs flat. JSON files that don't look
    like OpenAPI/Swagger are filtered out.
    """
    candidates: list[Path] = []
    sub = provider_dir / "swagger"
    if sub.is_dir():
        candidates.extend(sorted(sub.glob("*.json")))
    candidates.extend(sorted(provider_dir.glob("*.json")))

    specs: list[Path] = []
    seen: set[Path] = set()
    for p in candidates:
        rp = p.resolve()
        if rp in seen:
            continue
        seen.add(rp)
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and (
            "openapi" in data or "swagger" in data or "paths" in data
        ):
            specs.append(p)
    return specs


def parse_spec(path: Path) -> SpecSummary | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None

    info = data.get("info", {}) or {}
    paths_obj = data.get("paths", {}) or {}

    spec_paths: list[str] = []
    methods_per_path: dict[str, list[str]] = {}
    for raw_path, item in paths_obj.items():
        if not isinstance(item, dict):
            continue
        spec_paths.append(raw_path)
        methods_per_path[raw_path] = [
            m
            for m in ("get", "post", "put", "patch", "delete", "head", "options")
            if m in item and isinstance(item.get(m), dict)
        ]

    servers: list[str] = []
    if "servers" in data and isinstance(data["servers"], list):
        for s in data["servers"]:
            if isinstance(s, dict) and isinstance(s.get("url"), str):
                servers.append(s["url"])
    elif "host" in data:  # Swagger 2.0
        scheme = "https"
        schemes = data.get("schemes") or ["https"]
        if isinstance(schemes, list) and schemes:
            scheme = str(schemes[0])
        host = str(data.get("host", ""))
        base = str(data.get("basePath", "") or "")
        if host:
            servers.append(f"{scheme}://{host}{base}")

    return SpecSummary(
        file_name=path.name,
        title=str(info.get("title") or path.stem),
        version=str(info.get("version")) if info.get("version") is not None else None,
        spec_paths=spec_paths,
        methods_per_path=methods_per_path,
        servers=servers,
    )


def extract_hosts(specs: Iterable[SpecSummary]) -> list[str]:
    """Unique list of hostnames across all specs' servers."""
    seen: set[str] = set()
    ordered: list[str] = []
    for s in specs:
        for url in s.servers:
            host = _host_of(url)
            if host and host not in seen:
                seen.add(host)
                ordered.append(host)
    return ordered


def _host_of(url: str) -> str | None:
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return None
    return parsed.hostname


_NON_PUBLIC_SUFFIXES = (".internal", ".local", ".lan", ".intranet")
_NON_PUBLIC_HOSTS = {"localhost"}


def _is_likely_public_host(host: str) -> bool:
    """Filter out localhost / IP literals / .internal-style hosts that show up
    in swagger servers but never represent the public façade."""
    if not host or "." not in host:
        return False  # single-label hosts like 'localhost'
    if host in _NON_PUBLIC_HOSTS:
        return False
    lower = host.lower()
    if any(lower.endswith(s) for s in _NON_PUBLIC_SUFFIXES):
        return False
    # IPv4 literal (e.g. 127.0.0.1) — no apex domain to derive
    if all(part.isdigit() for part in host.split(".")):
        return False
    return True


def detect_base_domain(
    hosts: Iterable[str], dns_results: dict[str, HostDnsStatus]
) -> str | None:
    """Pick the shortest apex domain across hosts that *could* be public.

    Strategy:
        1. drop obviously non-public hosts (localhost, *.internal, IP literals)
        2. prefer hosts that publicly resolve right now;
        3. if none resolve, fall back to all public-shaped hosts (Inpay's
           internal-only *-backend-v2.<domain> still tells us the apex);
        4. return the shortest two-label apex (good for *.com / *.io and the
           common cases — does not handle *.co.uk specifically).
    """
    public_shaped = [h for h in hosts if _is_likely_public_host(h)]
    if not public_shaped:
        return None
    resolvable = [
        h for h in public_shaped
        if dns_results.get(h, HostDnsStatus(h, False)).resolves
    ]
    pool = resolvable or public_shaped
    domains = [_apex_domain(h) for h in pool]
    domains = [d for d in domains if d]
    if not domains:
        return None
    return min(domains, key=len)


def _apex_domain(host: str) -> str | None:
    if not host or "." not in host:
        return None
    parts = host.split(".")
    # crude two-label apex; works for *.inpay.com, *.example.org, etc.
    # Won't be right for *.co.uk, but providers we care about don't use those.
    if len(parts) <= 2:
        return host
    return ".".join(parts[-2:])


def generate_facade_candidates(base_domain: str) -> list[str]:
    """Return ordered list of likely public-façade base URLs to probe."""
    if not base_domain:
        return []
    envs = ("test", "sandbox", "uat", "dev")
    bases = [
        f"https://api.{base_domain}",
        *(f"https://{env}-api.{base_domain}" for env in envs),
        *(f"https://api-{env}.{base_domain}" for env in envs),
        f"https://{base_domain}",
    ]
    seen: set[str] = set()
    ordered: list[str] = []
    for b in bases:
        if b not in seen:
            seen.add(b)
            ordered.append(b)
    return ordered


def pick_sample_paths(
    spec: SpecSummary, max_n: int = 3
) -> list[tuple[str, str]]:
    """Pick up to max_n (method, path) pairs that are safe to probe.

    Strategy:
        - prefer GET (no body, no side effects)
        - if no GET, fall back to HEAD; otherwise POST is allowed only with
          known-safe path patterns (we intentionally exclude all POST/PUT/
          DELETE — a misclassified DELETE can corrupt sandbox data)
        - prefer paths that look distinct (different prefixes) so the verdict
          isn't drawn from one accidentally-special endpoint
    """
    gets: list[tuple[str, str]] = []
    for path, methods in spec.methods_per_path.items():
        if "get" in methods:
            gets.append(("get", path))

    # Diversify by first path segment so we don't probe 3 sibling endpoints
    by_prefix: dict[str, tuple[str, str]] = {}
    for method, path in gets:
        first_seg = "/".join(path.split("/")[:3])  # /a/b/{c}/d -> /a/b/{c}
        by_prefix.setdefault(first_seg, (method, path))

    diversified = list(by_prefix.values())
    leftovers = [mp for mp in gets if mp not in diversified]
    return (diversified + leftovers)[:max_n]


def substitute_placeholders(path: str) -> str:
    """Replace {placeholder} segments with safe filler values."""

    def repl(match: re.Match[str]) -> str:
        name = match.group(0)[1:-1].lower()
        if "uuid" in name or name.endswith("_id") or name == "id":
            return "00000000-0000-0000-0000-000000000000"
        if "reference" in name or "ref" in name:
            return "000000000000"
        if "number" in name or name.endswith("_no") or "code" in name:
            return "1"
        return "placeholder"

    return PLACEHOLDER_RE.sub(repl, path)


def strip_api_prefix(path: str) -> str:
    """Remove a leading /api/ or /api segment from a path."""
    if path.startswith("/api/"):
        return path[4:]  # keep the leading slash from the next segment
    if path == "/api":
        return "/"
    return path


def classify_status(
    status: int | None,
    location: str | None,
    content_type: str | None = None,
) -> str:
    """Map an HTTP response to one of the STATUS_* constants.

    The content_type matters for 2xx: a JSON API endpoint that exists returns
    JSON / octet-stream / xml. A 200 OK with HTML is almost always an edge
    proxy serving the marketing or docs page in lieu of a 404 — we treat it
    as STATUS_REDIRECT_DOCS so the verdict logic doesn't mistake it for a
    live route. (Inpay's prod api.inpay.com does this for any /api/... URL.)
    """
    if status is None:
        return STATUS_UNREACHABLE
    ct = (content_type or "").lower()
    is_html = "text/html" in ct
    if 200 <= status < 300:
        if is_html:
            return STATUS_REDIRECT_DOCS
        return STATUS_LIVE
    if status in (400, 401, 403, 405, 406, 409, 410, 415, 422):
        # Auth/validation rejections are still proof the route exists, even
        # when an HTML error page is rendered — JSON APIs commonly send HTML
        # for 401/403 from a CDN. Don't downgrade these.
        return STATUS_LIVE
    if status == 404:
        return STATUS_NOT_FOUND
    if 300 <= status < 400:
        loc = (location or "").lower()
        if any(hint in loc for hint in DOCS_HINTS) or loc.endswith("/"):
            return STATUS_REDIRECT_DOCS
        if not loc:
            # 3xx without Location is suspicious — treat as docs-redirect
            return STATUS_REDIRECT_DOCS
        return STATUS_REDIRECT_OTHER
    if 500 <= status < 600:
        return STATUS_SERVER_ERROR
    return STATUS_OTHER


def derive_strip_verdict(
    original: list[ProbeResult],
    stripped: list[ProbeResult],
    paths_with_api: int,
    paths_total: int,
) -> tuple[str, str]:
    """Decide what to do with this spec given probe outcomes.

    Returns (verdict_constant, human-readable rationale).
    """
    if paths_total == 0:
        return VERDICT_NO_PROBES, "spec has no paths"
    if not original:
        return VERDICT_NO_PROBES, "no GET endpoints to probe"

    orig_sem = [r.semantic for r in original]
    strip_sem = [r.semantic for r in stripped]

    has_api = paths_with_api > 0

    if not has_api:
        # No /api/ prefix anywhere — only check whether routes are public at all.
        if all(s in LIVE_LIKE for s in orig_sem):
            return (
                VERDICT_LIVE_AS_AUTHORED,
                f"all {len(original)} probes returned live status — public path matches swagger",
            )
        if all(s in DEAD_LIKE or s == STATUS_UNREACHABLE for s in orig_sem):
            return (
                VERDICT_NOT_PUBLIC,
                f"{len(original)} probes returned 404/redirect-to-docs/unreachable — namespace not exposed publicly",
            )
        live = sum(1 for s in orig_sem if s in LIVE_LIKE)
        return (
            VERDICT_INCONCLUSIVE,
            f"mixed probe outcomes ({live}/{len(orig_sem)} live) — manual review needed",
        )

    # Has /api/ prefix on at least one path. Compare original vs stripped.
    orig_live = sum(1 for s in orig_sem if s in LIVE_LIKE)
    orig_dead = sum(1 for s in orig_sem if s in DEAD_LIKE)
    strip_live = sum(1 for s in strip_sem if s in LIVE_LIKE)
    strip_dead = sum(1 for s in strip_sem if s in DEAD_LIKE)

    n = len(original)
    if strip_live == n and orig_dead >= 1 and orig_live == 0:
        return (
            VERDICT_STRIP_API,
            f"{strip_live}/{n} stripped probes are live, "
            f"{orig_dead}/{n} originals 404 / redirect-to-docs — "
            "edge proxy strips /api/",
        )
    if orig_live == n and strip_dead >= 1 and strip_live == 0:
        return (
            VERDICT_KEEP_API,
            f"{orig_live}/{n} original probes are live, "
            f"{strip_dead}/{n} stripped 404 — keep /api/ as authored",
        )
    if orig_dead == n and strip_dead == n:
        return (
            VERDICT_NOT_PUBLIC,
            "both original and stripped paths return 404/redirect — "
            "spec describes an internal-only namespace",
        )
    if orig_live >= 1 and strip_live >= 1:
        return (
            VERDICT_INCONCLUSIVE,
            f"both original ({orig_live}/{n}) and stripped ({strip_live}/{n}) "
            "appear live — could be alias; pick one and probe with credentials",
        )
    return (
        VERDICT_INCONCLUSIVE,
        f"mixed outcomes (orig live={orig_live}/{n} dead={orig_dead}, "
        f"strip live={strip_live}/{n} dead={strip_dead}) — manual review needed",
    )


def consistency_summary(spec: SpecSummary) -> tuple[int, int]:
    api = sum(1 for p in spec.spec_paths if p.startswith("/api/"))
    return api, len(spec.spec_paths)


# ---------------------------------------------------------------------------
# I/O wrappers (kept thin and easy to monkeypatch in tests)


def resolve_dns(host: str, timeout: float = 5.0) -> HostDnsStatus:
    socket.setdefaulttimeout(timeout)
    try:
        socket.gethostbyname(host)
        return HostDnsStatus(host=host, resolves=True)
    except socket.gaierror as e:
        return HostDnsStatus(host=host, resolves=False, error=str(e))
    except OSError as e:
        return HostDnsStatus(host=host, resolves=False, error=str(e))


def probe_url(url: str, method: str = "GET", timeout: float = 6.0) -> ProbeResult:
    ctx = ssl.create_default_context()
    req = urllib.request.Request(url, method=method)
    req.add_header("User-Agent", "code-rag-mcp validate_provider_paths/1.0")
    req.add_header("Accept", "application/json,*/*")
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            status = resp.status
            location = resp.headers.get("Location")
            content_type = resp.headers.get("Content-Type")
    except urllib.error.HTTPError as e:
        status = e.code
        location = e.headers.get("Location") if e.headers else None
        content_type = e.headers.get("Content-Type") if e.headers else None
    except urllib.error.URLError as e:
        return ProbeResult(
            url=url,
            method=method,
            status_code=None,
            location=None,
            semantic=STATUS_UNREACHABLE,
            error=str(e.reason if hasattr(e, "reason") else e),
        )
    except (TimeoutError, OSError) as e:
        return ProbeResult(
            url=url,
            method=method,
            status_code=None,
            location=None,
            semantic=STATUS_UNREACHABLE,
            error=str(e),
        )
    semantic = classify_status(status, location, content_type)
    return ProbeResult(
        url=url,
        method=method,
        status_code=status,
        location=location,
        content_type=content_type,
        semantic=semantic,
    )


# ---------------------------------------------------------------------------
# Orchestration


def pick_facade(
    candidates: list[str], timeout: float = 6.0
) -> tuple[str | None, list[tuple[str, ProbeResult]]]:
    """Probe each candidate base URL with a HEAD-like GET to /. Return the
    first one that responds with anything other than DNS failure, plus the
    full attempt log."""
    log: list[tuple[str, ProbeResult]] = []
    for base in candidates:
        result = probe_url(base.rstrip("/") + "/", method="GET", timeout=timeout)
        log.append((base, result))
        if result.semantic != STATUS_UNREACHABLE:
            return base, log
    return None, log


def run_spec_probes(
    spec: SpecSummary,
    facade: str,
    probe_limit: int,
    timeout: float,
) -> SpecProbeOutcome:
    samples = pick_sample_paths(spec, max_n=probe_limit)
    paths_with_api, paths_total = consistency_summary(spec)
    original_results: list[ProbeResult] = []
    stripped_results: list[ProbeResult] = []

    for method, raw_path in samples:
        concrete = substitute_placeholders(raw_path)
        url_orig = facade.rstrip("/") + concrete
        original_results.append(
            probe_url(url_orig, method=method.upper(), timeout=timeout)
        )
        if concrete.startswith("/api/"):
            stripped_path = strip_api_prefix(concrete)
            url_strip = facade.rstrip("/") + stripped_path
            stripped_results.append(
                probe_url(url_strip, method=method.upper(), timeout=timeout)
            )

    verdict, rationale = derive_strip_verdict(
        original_results, stripped_results, paths_with_api, paths_total
    )
    return SpecProbeOutcome(
        spec_name=spec.file_name,
        sample_method_paths=samples,
        original_results=original_results,
        stripped_results=stripped_results,
        paths_with_api_prefix=paths_with_api,
        paths_total=paths_total,
        verdict=verdict,
        rationale=rationale,
    )


# ---------------------------------------------------------------------------
# Reporting


def render_report(
    provider_dir: Path,
    specs: list[SpecSummary],
    dns_results: dict[str, HostDnsStatus],
    facade: str | None,
    facade_log: list[tuple[str, ProbeResult]],
    outcomes: list[SpecProbeOutcome],
) -> str:
    lines: list[str] = []
    lines.append(f"# Validation report — {provider_dir.name}")
    lines.append("")
    lines.append(
        f"Provider directory: `{provider_dir}`. Specs found: {len(specs)}. "
        f"Public façade: `{facade or 'NONE'}`."
    )
    lines.append("")

    lines.append("## DNS resolution of swagger-listed servers")
    lines.append("")
    if not dns_results:
        lines.append("_No servers declared in any spec._")
    else:
        lines.append("| Host | Resolves? | Note |")
        lines.append("|------|-----------|------|")
        for host, status in dns_results.items():
            ok = "✓" if status.resolves else "✗"
            note = "publicly resolvable" if status.resolves else (status.error or "no DNS")
            lines.append(f"| `{host}` | {ok} | {note} |")
    lines.append("")

    lines.append("## Public façade discovery")
    lines.append("")
    if facade:
        lines.append(f"Selected: `{facade}`")
    else:
        lines.append("**No public façade reachable** — every candidate failed.")
    lines.append("")
    lines.append("Probe log:")
    for base, result in facade_log:
        if result.status_code is not None:
            lines.append(f"- `{base}/` → {result.status_code} ({result.semantic})")
        else:
            lines.append(f"- `{base}/` → unreachable ({result.error})")
    lines.append("")

    lines.append("## Per-spec verdicts")
    lines.append("")
    if not outcomes:
        lines.append("_No specs probed (no façade)._")
    else:
        for o in outcomes:
            lines.append(f"### `{o.spec_name}` — **{o.verdict}**")
            lines.append("")
            lines.append(
                f"Paths: {o.paths_total} total, {o.paths_with_api_prefix} with `/api/` prefix."
            )
            lines.append("")
            lines.append(f"Rationale: {o.rationale}")
            lines.append("")
            if o.sample_method_paths:
                lines.append("Probes (truncated to sample):")
                for i, (method, raw) in enumerate(o.sample_method_paths):
                    orig = o.original_results[i] if i < len(o.original_results) else None
                    strip_r = o.stripped_results[i] if i < len(o.stripped_results) else None
                    if orig:
                        loc = f" → {orig.location}" if orig.location else ""
                        lines.append(
                            f"- `{method.upper()} {raw}` → "
                            f"{orig.status_code} ({orig.semantic}){loc}"
                        )
                    if strip_r:
                        stripped_path = strip_api_prefix(substitute_placeholders(raw))
                        loc = f" → {strip_r.location}" if strip_r.location else ""
                        lines.append(
                            f"  - stripped `{method.upper()} {stripped_path}` → "
                            f"{strip_r.status_code} ({strip_r.semantic}){loc}"
                        )
            lines.append("")

    lines.append("## Suggested mapping table (paste into provider index.md)")
    lines.append("")
    if not outcomes or not facade:
        lines.append("_skipped — no façade verdicts available._")
    else:
        lines.append("| Spec | Swagger path prefix | Public path prefix | Verdict |")
        lines.append("|------|---------------------|---------------------|---------|")
        for o in outcomes:
            sample_path = o.sample_method_paths[0][1] if o.sample_method_paths else "—"
            if o.verdict == VERDICT_STRIP_API:
                public = strip_api_prefix(sample_path)
            elif o.verdict in (VERDICT_KEEP_API, VERDICT_LIVE_AS_AUTHORED):
                public = sample_path
            elif o.verdict == VERDICT_NOT_PUBLIC:
                public = "**NOT PUBLIC**"
            else:
                public = "(manual review)"
            lines.append(
                f"| `{o.spec_name}` | `{sample_path}` | `{public}` | {o.verdict} |"
            )
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate scraped provider docs against the live public façade."
    )
    parser.add_argument("provider_dir", type=Path, help="profiles/{p}/docs/providers/{name}")
    parser.add_argument(
        "--public-host",
        type=str,
        default=None,
        help="Override façade base URL (e.g. https://test-api.inpay.com). "
        "If omitted, the validator auto-detects from swagger servers.",
    )
    parser.add_argument(
        "--probe-limit",
        type=int,
        default=3,
        help="Max GET paths to probe per spec (default 3).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=6.0,
        help="Per-request timeout in seconds (default 6).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Write the report to this file instead of stdout.",
    )
    args = parser.parse_args(argv)

    provider_dir: Path = args.provider_dir
    if not provider_dir.is_dir():
        print(f"ERROR: {provider_dir} is not a directory", file=sys.stderr)
        return 2

    spec_files = find_spec_files(provider_dir)
    if not spec_files:
        print(f"ERROR: no OpenAPI/Swagger JSON found under {provider_dir}", file=sys.stderr)
        return 2

    specs: list[SpecSummary] = []
    for sf in spec_files:
        s = parse_spec(sf)
        if s:
            specs.append(s)

    print(f"[1/4] Parsed {len(specs)} spec(s) from {provider_dir}", file=sys.stderr)

    hosts = extract_hosts(specs)
    print(f"[2/4] Resolving {len(hosts)} unique host(s)…", file=sys.stderr)
    dns_results: dict[str, HostDnsStatus] = {}
    for h in hosts:
        dns_results[h] = resolve_dns(h, timeout=args.timeout)

    if args.public_host:
        facade = args.public_host.rstrip("/")
        facade_log: list[tuple[str, ProbeResult]] = [
            (facade, probe_url(facade + "/", method="GET", timeout=args.timeout))
        ]
        if facade_log[0][1].semantic == STATUS_UNREACHABLE:
            print(f"WARNING: --public-host {facade} not reachable", file=sys.stderr)
    else:
        base_domain = detect_base_domain(hosts, dns_results)
        candidates = generate_facade_candidates(base_domain) if base_domain else []
        print(
            f"[3/4] Detected base domain: {base_domain!r}; probing "
            f"{len(candidates)} façade candidate(s)…",
            file=sys.stderr,
        )
        facade, facade_log = pick_facade(candidates, timeout=args.timeout)

    outcomes: list[SpecProbeOutcome] = []
    if facade:
        print(f"[4/4] Probing endpoints on {facade}…", file=sys.stderr)
        for s in specs:
            outcomes.append(
                run_spec_probes(s, facade, probe_limit=args.probe_limit, timeout=args.timeout)
            )
    else:
        print("[4/4] No façade reachable — skipping endpoint probes", file=sys.stderr)

    report = render_report(provider_dir, specs, dns_results, facade, facade_log, outcomes)

    if args.output:
        args.output.write_text(report, encoding="utf-8")
        print(f"Wrote {args.output}", file=sys.stderr)
    else:
        print(report)

    # Exit code: 0 if every spec is OK or NOT_PUBLIC (definitive), 1 if any
    # verdict is INCONCLUSIVE (manual review needed), 2 if no probes happened.
    if not outcomes:
        return 2
    bad = [o for o in outcomes if o.verdict == VERDICT_INCONCLUSIVE]
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
