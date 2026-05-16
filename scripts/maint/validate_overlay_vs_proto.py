#!/usr/bin/env python3
"""validate_overlay_vs_proto.py — implements C20 ruling.

Per debate-c20 ruling: proto = schema SoT; field-contracts.yaml = invariant SoT.
Every entry in field-contracts.yaml must EITHER reference a real proto path via
`proto:` field OR carry `non_proto: true` marker (for webhook bodies, gateway
composition, JS-only predicate semantics).

Output: list of unmarked entries. Exit 1 on violations.

Coverage check (proto field → overlay entry mention) is harder (needs proto parser);
left as TODO. This validator catches the easier direction: overlay → proto provenance.
"""
from __future__ import annotations
import re
import sys
from pathlib import Path

OVERLAY = Path.home() / ".code-rag-mcp/profiles/pay-com/docs/references/field-contracts.yaml"

# Lightweight YAML walker: top-level group, then per-entry block.
GROUP_RE = re.compile(r"^([a-zA-Z][\w-]*):\s*$")
ENTRY_RE = re.compile(r"^  ([a-zA-Z_][\w]*):\s*$")
ATTR_RE = re.compile(r"^    ([a-zA-Z_][\w]*):")


def main() -> int:
    if not OVERLAY.exists():
        print(f"FAIL: {OVERLAY} not found")
        return 1

    text = OVERLAY.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()

    current_group = None
    current_entry = None
    entry_attrs: dict[str, set[str]] = {}
    entry_locs: dict[str, int] = {}

    for i, line in enumerate(lines, start=1):
        if line.startswith("#") or not line.strip():
            continue
        m = GROUP_RE.match(line)
        if m and not line.startswith(" "):
            current_group = m.group(1)
            current_entry = None
            continue
        m = ENTRY_RE.match(line)
        if m:
            current_entry = f"{current_group}.{m.group(1)}"
            entry_attrs[current_entry] = set()
            entry_locs[current_entry] = i
            continue
        m = ATTR_RE.match(line)
        if m and current_entry:
            entry_attrs[current_entry].add(m.group(1))
            continue

    missing = []
    for entry, attrs in entry_attrs.items():
        if "proto" in attrs or "non_proto" in attrs:
            continue
        missing.append((entry, entry_locs[entry]))

    print(f"overlay entries checked: total={len(entry_attrs)} ok={len(entry_attrs) - len(missing)} unmarked={len(missing)}")
    if not missing:
        print("OK: every overlay entry carries `proto:` or `non_proto: true`.")
        return 0
    print("\nUnmarked entries (need `proto:` or `non_proto: true`):")
    for entry, lineno in missing[:30]:
        print(f"  field-contracts.yaml:{lineno}  {entry}")
    if len(missing) > 30:
        print(f"  ... and {len(missing) - 30} more")
    return 1


if __name__ == "__main__":
    sys.exit(main())
