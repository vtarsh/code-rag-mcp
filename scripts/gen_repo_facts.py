#!/usr/bin/env python3
"""Generate factual metadata from cloned repos.

Reads: $CODE_RAG_HOME/raw/*/
Writes: profiles/{active_profile}/generated/repo_facts.json

Machine-derivable facts only: package/version/deps/commit/proto RPCs/top-level tree.
No LLM. Safe to run in daytime (skip-vectors) mode.

Output is JSON only — NOT indexed by FTS5 (to avoid polluting base recall).
Sessions can read directly via Read tool, or downstream tools can parse it.
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(os.environ.get("CODE_RAG_HOME", os.path.expanduser("~/.code-rag")))


def _resolve_profile() -> str:
    env = os.environ.get("ACTIVE_PROFILE", "").strip()
    if env:
        return env
    f = BASE_DIR / ".active_profile"
    if f.exists():
        try:
            val = f.read_text(encoding="utf-8", errors="replace").strip()
            if val:
                return val
        except Exception:
            pass
    return "example"


PROFILE = _resolve_profile()
RAW_DIR = BASE_DIR / "raw"
OUT_DIR = BASE_DIR / "profiles" / PROFILE / "generated"
OUT_JSON = OUT_DIR / "repo_facts.json"

SKIP_DIRS = {"node_modules", ".git", "dist", "build", "vendor", "__pycache__", ".venv"}
PROTO_SERVICE_START_RE = re.compile(r"\bservice\s+(\w+)\s*\{")
PROTO_RPC_RE = re.compile(r"\brpc\s+(\w+)\s*\(\s*([\w.]+)\s*\)\s*returns\s*\(\s*([\w.]+)\s*\)")


def _read_text(p: Path) -> str | None:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None


def _extract_balanced_body(text: str, open_idx: int) -> str:
    depth = 0
    for i in range(open_idx, len(text)):
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[open_idx + 1 : i]
    return text[open_idx + 1 :]


def git_last_commit(repo: Path) -> dict | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "log", "-1", "--format=%cI|%ct|%an|%s"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode != 0 or not out.stdout.strip():
            return None
        iso, epoch, author, subj = out.stdout.strip().split("|", 3)
        return {
            "iso": iso,
            "epoch": int(epoch),
            "author": author,
            "subject": subj[:120],
        }
    except Exception:
        return None


def parse_package_json(p: Path) -> dict:
    text = _read_text(p)
    if text is None:
        return {}
    try:
        data = json.loads(text)
    except Exception:
        return {}
    return {
        "name": data.get("name"),
        "version": data.get("version"),
        "deps": sorted((data.get("dependencies") or {}).keys()),
        "dev_deps_count": len(data.get("devDependencies") or {}),
    }


def parse_go_mod(p: Path) -> dict:
    text = _read_text(p)
    if text is None:
        return {}
    mod = re.search(r"^module\s+(\S+)", text, re.M)
    go = re.search(r"^go\s+(\S+)", text, re.M)
    # Only count deps inside a top-level require(...) block or bare `require` lines.
    # Ignore `replace`/`exclude`/`retract` directives.
    require_block = re.search(r"require\s*\(([^)]*)\)", text, re.S)
    deps_count = 0
    if require_block:
        for line in require_block.group(1).splitlines():
            if re.match(r"\s*[\w./-]+\s+v\S+", line) and "//" not in line.split()[0]:
                deps_count += 1
    for line in text.splitlines():
        if re.match(r"^require\s+[\w./-]+\s+v\S+", line):
            deps_count += 1
    return {
        "name": mod.group(1) if mod else None,
        "go_version": go.group(1) if go else None,
        "deps_count": deps_count,
    }


def parse_proto_files(repo: Path) -> list:
    services = []
    protos = []
    try:
        protos = sorted(repo.rglob("*.proto"))
    except Exception:
        return services
    for proto in protos:
        if any(part in SKIP_DIRS for part in proto.parts):
            continue
        text = _read_text(proto)
        if text is None:
            continue
        for svc in PROTO_SERVICE_START_RE.finditer(text):
            brace_idx = svc.end() - 1
            body = _extract_balanced_body(text, brace_idx)
            rpcs = [
                {"name": m.group(1), "input": m.group(2), "output": m.group(3)}
                for m in PROTO_RPC_RE.finditer(body)
            ]
            services.append({
                "file": str(proto.relative_to(repo)),
                "service": svc.group(1),
                "rpcs": rpcs,
            })
    return services


def top_level(repo: Path) -> list:
    try:
        return sorted(
            p.name for p in repo.iterdir()
            if not p.name.startswith(".") and p.name not in SKIP_DIRS
        )
    except Exception:
        return []


def extract_repo(repo: Path) -> dict:
    facts: dict = {
        "name": repo.name,
        "top_level": top_level(repo),
        "last_commit": git_last_commit(repo),
    }
    pkg = repo / "package.json"
    if pkg.exists():
        facts["language"] = "js_ts"
        facts["package_json"] = parse_package_json(pkg)
    gomod = repo / "go.mod"
    if gomod.exists():
        facts.setdefault("language", "go")
        facts["go_mod"] = parse_go_mod(gomod)
    if (repo / "pyproject.toml").exists() or (repo / "setup.py").exists():
        facts.setdefault("language", "python")
    protos = parse_proto_files(repo)
    if protos:
        facts["proto_services"] = protos
    return facts


def main() -> int:
    if not RAW_DIR.is_dir():
        print(f"no raw dir at {RAW_DIR}, skipping", file=sys.stderr)
        return 0
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    repos = sorted(p for p in RAW_DIR.iterdir() if p.is_dir() and not p.name.startswith("."))
    facts = []
    for repo in repos:
        try:
            facts.append(extract_repo(repo))
        except Exception as e:
            print(f"  ⚠️ {repo.name}: {e}", file=sys.stderr)
    OUT_JSON.write_text(json.dumps(facts, indent=2, default=str))
    print(f"✓ {len(facts)} repos → {OUT_JSON.relative_to(BASE_DIR)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
