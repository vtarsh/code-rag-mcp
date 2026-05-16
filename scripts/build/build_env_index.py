#!/usr/bin/env python3
"""
Build env var index from .env.example files and consts.js defaults.

Parses:
  1. extracted/*/env/.env.example — key=value pairs
  2. raw/*/src/consts.js or raw/*/consts.js — destructured defaults
  3. Detects map-type env vars (comma-separated key=value inside one var)

Output: env_vars table in knowledge.db + searchable chunks for map-type vars
"""

import json
import os
import re
import sqlite3
from pathlib import Path

BASE_DIR = Path(os.getenv("CODE_RAG_HOME", Path.home() / ".code-rag"))
EXTRACTED_DIR = BASE_DIR / "extracted"
RAW_DIR = BASE_DIR / "raw"
DB_PATH = BASE_DIR / "db" / "knowledge.db"
_profile = os.getenv("ACTIVE_PROFILE", "")
if not _profile:
    _ap = BASE_DIR / ".active_profile"
    _profile = _ap.read_text().strip() if _ap.exists() else ""
_profile_registry = BASE_DIR / "profiles" / _profile / "docs" / "domain_registry.yaml" if _profile else None
DOMAIN_REGISTRY_FILE = (
    _profile_registry
    if (_profile_registry and _profile_registry.exists())
    else BASE_DIR / "docs" / "domain_registry.yaml"
)


def init_env_table(conn: sqlite3.Connection):
    """Create env_vars table if not exists."""
    conn.execute("DROP TABLE IF EXISTS env_vars")
    conn.execute("""
        CREATE TABLE env_vars (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repo TEXT NOT NULL,
            var_name TEXT NOT NULL,
            raw_value TEXT,
            parsed_value TEXT,  -- JSON for map-type vars
            source TEXT,        -- 'env_example', 'consts_js', 'consts_go'
            is_map BOOLEAN DEFAULT 0,
            UNIQUE(repo, var_name, source)
        )
    """)
    conn.execute("CREATE INDEX idx_env_vars_repo ON env_vars(repo)")
    conn.execute("CREATE INDEX idx_env_vars_name ON env_vars(var_name)")
    conn.commit()


def parse_env_example(file_path: Path) -> list[dict]:
    """Parse .env.example file into key-value pairs."""
    entries = []
    for line in file_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("'\"")
            if key:
                entries.append({"var_name": key, "raw_value": value})
    return entries


def parse_consts_js(file_path: Path) -> list[dict]:
    """Parse consts.js for destructured env vars with defaults.

    Pattern: VAR_NAME = 'default_value',
    """
    entries = []
    content = file_path.read_text()

    # Match: VAR_NAME = 'value' or VAR_NAME = "value" or VAR_NAME = value
    pattern = re.compile(
        r"^\s+(\w+)\s*=\s*(?:'([^']*)'|\"([^\"]*)\"|(\S+?))\s*[,;]?\s*$",
        re.MULTILINE,
    )

    for m in pattern.finditer(content):
        var_name = m.group(1)
        value = m.group(2) or m.group(3) or m.group(4) or ""
        # Skip non-env-var patterns (lowercase, very short names, etc.)
        if not var_name.isupper() and "_" not in var_name:
            continue
        if len(var_name) < 3:
            continue
        entries.append({"var_name": var_name, "raw_value": value})

    return entries


def detect_map_type(value: str) -> dict | None:
    """Detect if a value is a map-type env var (key=value,key=value,...).

    Returns parsed dict if map-type, None otherwise.
    """
    if not value or "=" not in value or "," not in value:
        return None

    parts = value.split(",")
    parsed = {}
    for part in parts:
        part = part.strip()
        if "=" not in part:
            return None  # Not a valid map
        k, _, v = part.partition("=")
        k = k.strip()
        v = v.strip()
        if not k:
            return None
        parsed[k] = v

    if len(parsed) < 2:
        return None  # Need at least 2 entries to be a map

    return parsed


def load_domain_registry() -> dict[str, str]:
    """Load domain → repo mapping for URL resolution."""
    if not DOMAIN_REGISTRY_FILE.is_file():
        return {}

    mapping = {}
    text = DOMAIN_REGISTRY_FILE.read_text()
    current_domain = ""

    for line in text.splitlines():
        m = re.match(r'\s+-\s+domain:\s+"(.+)"', line)
        if m:
            current_domain = m.group(1)
        m = re.match(r"\s+repo:\s+(\S+)", line)
        if m and current_domain:
            # Expand {env} variants
            for env in ["dev", "staging", ""]:
                if env:
                    expanded = current_domain.replace("{env}.", f"{env}.")
                else:
                    expanded = current_domain.replace("{env}.", "")
                mapping[expanded] = m.group(1)

    return mapping


def build_env_index():
    """Main: scan all repos, parse env vars, store in DB."""
    print("Building env var index...")

    conn = sqlite3.connect(str(DB_PATH))
    init_env_table(conn)

    domain_map = load_domain_registry()
    total_vars = 0
    total_maps = 0
    repos_scanned = 0

    # Get list of repos from extracted dir
    if not EXTRACTED_DIR.exists():
        print("  No extracted/ directory found")
        conn.close()
        return

    repo_dirs = sorted(d for d in EXTRACTED_DIR.iterdir() if d.is_dir() and not d.name.startswith("_"))

    for repo_dir in repo_dirs:
        repo_name = repo_dir.name
        env_entries: list[dict] = []

        # 1. Parse .env.example from extracted
        env_example = repo_dir / "env" / ".env.example"
        if env_example.exists():
            for entry in parse_env_example(env_example):
                entry["source"] = "env_example"
                env_entries.append(entry)

        # 2. Parse consts.js from extracted/*/env/consts.js
        consts_extracted = repo_dir / "env" / "consts.js"
        if consts_extracted.exists():
            for entry in parse_consts_js(consts_extracted):
                entry["source"] = "consts_js"
                env_entries.append(entry)

        # 3. Parse consts.js from raw (has full defaults not always in extracted)
        for consts_raw in [
            RAW_DIR / repo_name / "src" / "consts.js",
            RAW_DIR / repo_name / "consts.js",
        ]:
            if consts_raw.exists():
                for entry in parse_consts_js(consts_raw):
                    # Only add if not already found from extracted
                    existing_names = {e["var_name"] for e in env_entries}
                    if entry["var_name"] not in existing_names:
                        entry["source"] = "consts_js_raw"
                        env_entries.append(entry)

        if not env_entries:
            continue

        repos_scanned += 1

        # Detect map-type vars and insert
        for entry in env_entries:
            parsed_map = detect_map_type(entry["raw_value"])
            is_map = parsed_map is not None
            parsed_json = json.dumps(parsed_map) if parsed_map else None

            if is_map:
                total_maps += 1

            conn.execute(
                "INSERT OR IGNORE INTO env_vars(repo, var_name, raw_value, parsed_value, source, is_map) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (repo_name, entry["var_name"], entry["raw_value"], parsed_json, entry["source"], is_map),
            )
            total_vars += 1

    conn.commit()

    # Create searchable chunks for map-type env vars
    map_chunks = 0
    map_rows = conn.execute("SELECT repo, var_name, raw_value, parsed_value FROM env_vars WHERE is_map = 1").fetchall()

    for repo, var_name, raw_value, parsed_json in map_rows:
        parsed = json.loads(parsed_json) if parsed_json else {}

        # Resolve URLs to repos via domain registry
        resolved_entries = []
        for key, url in parsed.items():
            target_repo = None
            for domain, domain_repo in domain_map.items():
                if domain in url:
                    target_repo = domain_repo
                    break

            if target_repo:
                resolved_entries.append(f"  {key} = {url} → {target_repo}")
            else:
                resolved_entries.append(f"  {key} = {url}")

        content = f"[Env Map: {repo}] {var_name}\nRaw: {raw_value}\nEntries:\n" + "\n".join(resolved_entries) + "\n"

        # Check if chunks table exists and insert
        try:
            conn.execute(
                "INSERT INTO chunks(content, repo_name, file_path, file_type, chunk_type, language) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (content, repo, f"env/{var_name}", "env_map", "env_map_entry", "env"),
            )
            map_chunks += 1
        except sqlite3.OperationalError:
            pass  # chunks table might not exist if running standalone

    conn.commit()

    # Summary
    print(f"  Repos scanned: {repos_scanned}")
    print(f"  Total env vars: {total_vars}")
    print(f"  Map-type vars: {total_maps}")
    print(f"  Searchable chunks: {map_chunks}")

    # Show key findings
    if map_rows:
        print("\n  Key map-type env vars found:")
        for repo, var_name, raw_value, _ in map_rows[:10]:
            print(f"    {repo}: {var_name} = {raw_value[:80]}...")

    conn.close()
    print("\nDone!")


if __name__ == "__main__":
    build_env_index()
