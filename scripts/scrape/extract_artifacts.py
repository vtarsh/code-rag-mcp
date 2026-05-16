#!/usr/bin/env python3
"""
Phase 1, Step 1.2: Artifact Extractor
Extracts structured artifacts from cloned repos into ~/.code-rag/extracted/

Artifact types:
- proto: .proto files (gRPC service definitions)
- docs: CLAUDE.md, README.md, docs/ files
- config: package.json, go.mod, tsconfig.json
- env: .env.example, consts.js, env.ts (service config, env vars)
- k8s: deployment.yaml, service.yaml, kustomization.yaml
- methods: methods/*.js, src/methods/*.ts (gRPC handlers)
- workflows: Temporal workflow definitions
- webhooks: webhook/callback route handlers
- ci: .github/workflows/*.yml (CI/CD pipeline info)
"""

import json
import os
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

BASE_DIR = Path(os.getenv("CODE_RAG_HOME", Path.home() / ".code-rag"))
RAW_DIR = BASE_DIR / "raw"
EXTRACTED_DIR = BASE_DIR / "extracted"
STATE_FILE = BASE_DIR / "repo_state.json"
EXTRACT_LOG = BASE_DIR / "extract_log.json"

# Load org config (profile-aware)
_profile = os.getenv("ACTIVE_PROFILE", "")
if not _profile:
    _ap = BASE_DIR / ".active_profile"
    _profile = _ap.read_text().strip() if _ap.exists() else ""
_profile_config = BASE_DIR / "profiles" / _profile / "config.json" if _profile else None
_legacy_config = BASE_DIR / "config.json"
_config_path = _profile_config if (_profile_config and _profile_config.exists()) else _legacy_config
_config = json.loads(_config_path.read_text()) if _config_path.exists() else {}
_org = _config.get("org", "my-org")
NPM_SCOPE = _config.get("npm_scope", f"@{_org}")

# Max file size to extract (skip generated/huge files)
MAX_FILE_SIZE = 200 * 1024  # 200KB

# Patterns to skip
SKIP_DIRS = {
    "node_modules",
    ".git",
    "dist",
    "build",
    "coverage",
    ".nyc_output",
    ".cache",
    "vendor",
    "__pycache__",
    "types/generated",  # auto-generated TS types
}

SKIP_FILES = {
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
}

# Frontend source extraction (src_frontend artifact type)
FE_EXTS = {".ts", ".tsx", ".js", ".jsx", ".mjs"}
FE_SKIP_DIRS = {
    "__tests__",
    "__mocks__",
    "__snapshots__",
    "generated",  # auto-generated code (GraphQL codegen, OpenAPI, etc.)
    "stories",
}
# Extra suffixes to skip for FE pass (tests, stories, generated types)
FE_SKIP_SUFFIXES = (
    ".test.ts",
    ".test.tsx",
    ".test.js",
    ".test.jsx",
    ".spec.ts",
    ".spec.tsx",
    ".spec.js",
    ".spec.jsx",
    ".stories.ts",
    ".stories.tsx",
    ".stories.js",
    ".stories.jsx",
    ".d.ts",
)


def _fe_is_skippable(path: Path) -> bool:
    """FE-specific skip: honors base filters plus test/stories/generated."""
    if is_skippable(path):
        return True
    parts = set(path.parts)
    if parts & FE_SKIP_DIRS:
        return True
    name = path.name
    return any(name.endswith(suf) for suf in FE_SKIP_SUFFIXES)


def _iter_src_roots(repo_dir: Path):
    """Yield (src_root, rel_prefix) pairs to scan for frontend source.

    Strategy:
    - Top-level `src/`  -> prefix `src`
    - Monorepo `packages/*/src/` -> prefix `packages/<pkg>/src`
    - Monorepo `apps/*/src/`    -> prefix `apps/<app>/src`

    `rel_prefix` is the path under extracted/{repo}/ where files are written,
    preserving files_changed path shape (e.g. backoffice-web/src/Pages/X.tsx).
    """
    top_src = repo_dir / "src"
    if top_src.is_dir():
        yield top_src, Path("src")

    for mono in ("packages", "apps"):
        mono_dir = repo_dir / mono
        if not mono_dir.is_dir():
            continue
        for sub in mono_dir.iterdir():
            if not sub.is_dir() or sub.name.startswith("."):
                continue
            sub_src = sub / "src"
            if sub_src.is_dir():
                yield sub_src, Path(mono) / sub.name / "src"


def extract_src_frontend(repo_dir: Path, out_dir: Path) -> int:
    """Copy frontend source (.ts/.tsx/.js/.jsx/.mjs) from src/ trees.

    Preserves relative structure under extracted/{repo}/{src|packages/<p>/src|...}
    so Jira files_changed entries like `backoffice-web/src/Pages/X.tsx` resolve
    to `extracted/backoffice-web/src/Pages/X.tsx`.

    Auto-detect: only runs if repo has at least one src/ tree (top-level or
    inside packages/apps monorepo).
    """
    count = 0
    any_root = False
    for src_root, rel_prefix in _iter_src_roots(repo_dir):
        any_root = True
        for f in src_root.rglob("*"):
            if not f.is_file():
                continue
            if f.suffix not in FE_EXTS:
                continue
            if _fe_is_skippable(f):
                continue
            rel = f.relative_to(src_root)
            dst = out_dir / rel_prefix / rel
            safe_copy(f, dst)
            count += 1
    if not any_root:
        return 0
    return count


def is_skippable(path: Path) -> bool:
    """Check if path should be skipped."""
    parts = set(path.parts)
    if parts & SKIP_DIRS:
        return True
    if path.name in SKIP_FILES:
        return True
    return path.stat().st_size > MAX_FILE_SIZE


def safe_copy(src: Path, dst: Path):
    """Copy file, creating parent dirs as needed."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def extract_repo(repo_dir: Path, out_dir: Path) -> dict:
    """Extract artifacts from a single repo. Returns stats."""
    stats = {
        "proto": 0,
        "docs": 0,
        "config": 0,
        "env": 0,
        "k8s": 0,
        "methods": 0,
        "workflows": 0,
        "ci": 0,
        "libs": 0,
        "routes": 0,
        "services": 0,
        "handlers": 0,
        "utils": 0,
        "consts": 0,
        "src_frontend": 0,
    }

    if not repo_dir.is_dir():
        return stats

    # --- Proto files ---
    for proto in repo_dir.rglob("*.proto"):
        if is_skippable(proto):
            continue
        rel = proto.relative_to(repo_dir)
        safe_copy(proto, out_dir / "proto" / rel)
        stats["proto"] += 1

    # --- Docs: CLAUDE.md, README.md, docs/ ---
    for doc_name in ["CLAUDE.md", "README.md", "AI-CODING-GUIDE.md"]:
        doc = repo_dir / doc_name
        if doc.exists() and doc.stat().st_size <= MAX_FILE_SIZE:
            safe_copy(doc, out_dir / "docs" / doc_name)
            stats["docs"] += 1

    docs_dir = repo_dir / "docs"
    if docs_dir.is_dir():
        for doc in docs_dir.rglob("*.md"):
            if not is_skippable(doc):
                rel = doc.relative_to(repo_dir)
                safe_copy(doc, out_dir / "docs" / rel)
                stats["docs"] += 1

    # --- Config: package.json, go.mod, tsconfig.json ---
    for cfg_name in ["package.json", "go.mod", "go.sum", "tsconfig.json"]:
        cfg = repo_dir / cfg_name
        if cfg.exists() and cfg.stat().st_size <= MAX_FILE_SIZE:
            safe_copy(cfg, out_dir / "config" / cfg_name)
            stats["config"] += 1

    # --- Env: .env.example, consts.js, consts.ts, src/env.ts, src/consts.ts ---
    for env_name in [".env.example", "consts.js", "consts.ts"]:
        env = repo_dir / env_name
        if env.exists():
            safe_copy(env, out_dir / "env" / env_name)
            stats["env"] += 1
    for env_name in ["env.ts", "consts.ts"]:
        env = repo_dir / "src" / env_name
        if env.exists():
            safe_copy(env, out_dir / "env" / f"src_{env_name}")
            stats["env"] += 1

    # --- K8s configs ---
    for pattern in ["*.yaml", "*.yml"]:
        for k8s in repo_dir.rglob(pattern):
            if is_skippable(k8s):
                continue
            rel_str = str(k8s.relative_to(repo_dir))
            # Only k8s-related yamls (not CI, not configs)
            if any(
                kw in rel_str.lower()
                for kw in [
                    "deploy",
                    "service.yaml",
                    "service.yml",
                    "kustomization",
                    "helmrelease",
                    "ingress",
                    "configmap",
                    "namespace",
                ]
            ):
                rel = k8s.relative_to(repo_dir)
                safe_copy(k8s, out_dir / "k8s" / rel)
                stats["k8s"] += 1

    # --- Methods (gRPC handlers) ---
    # Old JS: methods/*.js
    methods_dir = repo_dir / "methods"
    if methods_dir.is_dir():
        for m in methods_dir.glob("*.js"):
            if not is_skippable(m):
                safe_copy(m, out_dir / "methods" / m.name)
                stats["methods"] += 1

    # New TS: src/methods/*.ts
    methods_dir_ts = repo_dir / "src" / "methods"
    if methods_dir_ts.is_dir():
        for m in methods_dir_ts.glob("*.ts"):
            if not is_skippable(m):
                safe_copy(m, out_dir / "methods" / m.name)
                stats["methods"] += 1

    # --- Source code: libs, routes, services, handlers, utils ---
    # These directories contain business logic, routing, and helper code
    SOURCE_SUFFIXES = {".js", ".ts", ".go"}
    source_dirs = [
        ("libs", [repo_dir / "libs", repo_dir / "src" / "libs"]),
        ("routes", [repo_dir / "src" / "routes", repo_dir / "routes"]),
        (
            "services",
            [
                repo_dir / "src" / "services",
                repo_dir / "services",
                repo_dir / "src" / "service",  # Go convention (singular)
            ],
        ),
        ("handlers", [repo_dir / "src" / "handlers", repo_dir / "handlers"]),
        (
            "utils",
            [
                repo_dir / "src" / "utils",
                repo_dir / "utils",
                repo_dir / "src" / "helpers",
                repo_dir / "helpers",
            ],
        ),
        ("consts", [repo_dir / "src" / "consts", repo_dir / "consts"]),
        # Go-specific directories
        (
            "services",
            [
                repo_dir / "cmd",
                repo_dir / "pkg",
                repo_dir / "internal",
                repo_dir / "specs",
            ],
        ),
    ]

    for dir_type, dirs in source_dirs:
        for src_dir in dirs:
            if src_dir.is_dir():
                for f in src_dir.rglob("*"):
                    if f.is_file() and f.suffix in SOURCE_SUFFIXES and not is_skippable(f):
                        rel = f.relative_to(src_dir)
                        safe_copy(f, out_dir / dir_type / rel)
                        stats[dir_type] += 1

    # --- Go/TS entry points at repo root ---
    for entry_name in ["main.go", "main.ts", "server.ts", "index.ts"]:
        entry = repo_dir / entry_name
        if entry.exists() and not is_skippable(entry):
            safe_copy(entry, out_dir / "services" / entry_name)
            stats["services"] += 1

    # --- Go health check (common pattern) ---
    health_dir = repo_dir / "src" / "health"
    if health_dir.is_dir():
        for f in health_dir.rglob("*.go"):
            if not is_skippable(f):
                rel = f.relative_to(health_dir)
                safe_copy(f, out_dir / "services" / "health" / rel)
                stats["services"] += 1

    # --- Go types ---
    types_dir = repo_dir / "src" / "types"
    if types_dir.is_dir():
        for f in types_dir.rglob("*.go"):
            if not is_skippable(f):
                rel = f.relative_to(types_dir)
                safe_copy(f, out_dir / "libs" / "types" / rel)
                stats["libs"] += 1

    # --- Temporal workflow definitions ---
    # Look for workflow patterns: workflow.js/ts, activities.js/ts, signals
    for suffix in ["*.js", "*.ts"]:
        for f in repo_dir.rglob(suffix):
            if is_skippable(f):
                continue
            rel_str = str(f.relative_to(repo_dir))
            if any(
                kw in rel_str.lower()
                for kw in [
                    "workflow",
                    "activities",
                    "signals",
                    "temporal",
                ]
            ):
                # Already captured under methods/libs? Store separately
                rel = f.relative_to(repo_dir)
                safe_copy(f, out_dir / "workflows" / rel)
                stats["workflows"] += 1

    # --- CI/CD ---
    ci_dir = repo_dir / ".github" / "workflows"
    if ci_dir.is_dir():
        for ci in ci_dir.iterdir():
            if ci.is_file() and ci.suffix in (".yml", ".yaml", ".template"):
                safe_copy(ci, out_dir / "ci" / ci.name)
                stats["ci"] += 1

    # --- Frontend source (src_frontend) ---
    # Auto-detect: copy all .ts/.tsx/.js/.jsx/.mjs under src/ (and monorepo
    # packages/*/src, apps/*/src), preserving relative paths so Jira
    # files_changed entries resolve 1:1 to extracted paths. Training relies
    # on this for FE tickets (backoffice-web, graphql, next-web-*, etc.).
    stats["src_frontend"] += extract_src_frontend(repo_dir, out_dir)

    return stats


def detect_repo_type(repo_dir: Path) -> str:
    """Detect repo type based on structure."""
    has_tsconfig = (repo_dir / "tsconfig.json").exists()
    has_src = (repo_dir / "src").is_dir()
    has_go_mod = (repo_dir / "go.mod").exists()
    has_package_json = (repo_dir / "package.json").exists()

    name = repo_dir.name

    if "boilerplate" in name:
        return "boilerplate"
    if "workflow" in name or "temporal" in name:
        return "temporal-workflow"
    if name.startswith("grpc-"):
        if has_tsconfig or has_src:
            return "grpc-service-ts"
        elif has_go_mod:
            return "grpc-service-go"
        else:
            return "grpc-service-js"
    if "libs-" in name or "node-libs-" in name:
        return "library"
    if name.startswith("github-"):
        return "ci-actions"
    if "flux2-" in name:
        return "gitops"
    if has_package_json:
        return "node-service"
    if has_go_mod:
        return "go-service"
    return "unknown"


def main():
    if not RAW_DIR.exists():
        print(f"Error: {RAW_DIR} does not exist. Run clone_repos.sh first.")
        sys.exit(1)

    # Parse --repos flag for incremental extraction
    only_repos = None
    for arg in sys.argv[1:]:
        if arg.startswith("--repos="):
            only_repos = set(arg.split("=", 1)[1].split(","))

    # Load state for tracking which repos have been extracted
    state = {}
    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text())

    all_repos = sorted([d for d in RAW_DIR.iterdir() if d.is_dir() and (d / ".git").is_dir()])

    if only_repos:
        repos = [d for d in all_repos if d.name in only_repos]
        print(f"Incremental extraction: {len(repos)} of {len(all_repos)} repos")
    else:
        repos = all_repos
        print(f"Full extraction: {len(repos)} repos")

    # Load existing index for incremental mode (preserve unchanged repos)
    meta_file = EXTRACTED_DIR / "_index.json"
    if only_repos and meta_file.exists():
        repo_meta = json.loads(meta_file.read_text())
    else:
        repo_meta = {}

    total_stats = {}

    for i, repo_dir in enumerate(repos, 1):
        repo_name = repo_dir.name
        out_dir = EXTRACTED_DIR / repo_name

        # Clean previous extraction for this repo
        if out_dir.exists():
            shutil.rmtree(out_dir)

        print(f"  [{i}/{len(repos)}] {repo_name}...", end="", flush=True)

        stats = extract_repo(repo_dir, out_dir)
        total = sum(stats.values())
        print(f" {total} artifacts")

        # Detect repo type
        repo_type = detect_repo_type(repo_dir)

        # Extract dependencies from package.json
        deps = []
        pkg_json = repo_dir / "package.json"
        if pkg_json.exists():
            try:
                pkg = json.loads(pkg_json.read_text())
                all_deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
                deps = [d for d in all_deps if d.startswith(f"{NPM_SCOPE}/")]
            except (json.JSONDecodeError, KeyError):
                pass

        repo_meta[repo_name] = {
            "type": repo_type,
            "sha": state.get(repo_name, "unknown"),
            "artifacts": stats,
            "org_deps": deps,
        }

        for k, v in stats.items():
            total_stats[k] = total_stats.get(k, 0) + v

    # Write metadata index
    meta_file.parent.mkdir(parents=True, exist_ok=True)
    meta_file.write_text(json.dumps(repo_meta, indent=2))

    # Write log
    log = {
        "date": datetime.now(UTC).isoformat(),
        "repos_processed": len(repos),
        "incremental": only_repos is not None,
        "artifacts": total_stats,
        "repo_types": {},
    }
    for meta in repo_meta.values():
        t = meta["type"]
        log["repo_types"][t] = log["repo_types"].get(t, 0) + 1

    Path(EXTRACT_LOG).write_text(json.dumps(log, indent=2))

    print("\n=== Extraction Summary ===")
    if only_repos:
        print(f"Mode: incremental ({len(repos)} repos)")
    else:
        print(f"Mode: full ({len(repos)} repos)")
    print("Artifacts by type:")
    for k, v in sorted(total_stats.items(), key=lambda x: -x[1]):
        print(f"  {k:12s}: {v}")
    print(f"\nMetadata: {meta_file}")
    print("========================")


if __name__ == "__main__":
    main()
