#!/usr/bin/env python3
"""Validate AGENTS.md files (root + profile) against the actual filesystem.

Read-only script — reports inconsistencies only, no file modifications.
Exit code 0 if pass, 1 if any FAIL.
"""

from __future__ import annotations

import fnmatch
import re
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path("/Users/vaceslavtarsevskij/.code-rag-mcp")
ROOT_AGENTS = REPO_ROOT / "AGENTS.md"
PROFILE_AGENTS = REPO_ROOT / "profiles" / "pay-com" / "AGENTS.md"

STANDARD_IGNORES = {
    ".git",
    "__pycache__",
    ".DS_Store",
    "node_modules",
    ".venv",
    "venv",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".idea",
    ".vscode",
    "*.pyc",
    "*.pyo",
    "*.egg-info",
    ".gitattributes",
    "*.pid",
}


@dataclass
class Finding:
    category: str
    message: str
    line: int = 0
    is_error: bool = True


class HealthChecker:
    def __init__(self) -> None:
        self.findings: list[Finding] = []
        self.warnings: list[Finding] = []
        self.verified_links = 0
        self.broken_links = 0
        self.warn_links = 0

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _run(self, cmd: list[str], cwd: Path = REPO_ROOT) -> tuple[int, str]:
        result = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)
        return result.returncode, result.stdout

    def _read(self, path: Path) -> str:
        return path.read_text(encoding="utf-8")

    def _lines(self, path: Path) -> list[str]:
        return self._read(path).splitlines()

    def _add(self, finding: Finding) -> None:
        if finding.is_error:
            self.findings.append(finding)
        else:
            self.warnings.append(finding)

    # ------------------------------------------------------------------
    # Link extraction
    # ------------------------------------------------------------------
    @staticmethod
    def _extract_md_links(text: str) -> Iterable[tuple[str, int]]:
        # [text](url)  but not ![...](...)
        for line_no, line in enumerate(text.splitlines(), 1):
            for m in re.finditer(r"(?<!\!)\[([^\]]+)\]\(([^)]+)\)", line):
                yield m.group(2), line_no

    @staticmethod
    def _extract_wikilinks(text: str) -> Iterable[tuple[str, int]]:
        for line_no, line in enumerate(text.splitlines(), 1):
            for m in re.finditer(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]", line):
                yield m.group(1), line_no

    @staticmethod
    def _resolve_link(raw: str, base_dir: Path) -> Path | None:
        if not raw:
            return None
        # External URLs
        if raw.startswith(("http://", "https://", "mailto:", "ftp://")):
            return None
        # Strip anchor / query
        if "#" in raw:
            raw = raw.split("#")[0]
        if "?" in raw:
            raw = raw.split("?")[0]
        if not raw:
            return None
        target = (base_dir / raw).resolve()
        return target

    # ------------------------------------------------------------------
    # 1. Link verification
    # ------------------------------------------------------------------
    def check_links(self, agents_path: Path) -> None:
        text = self._read(agents_path)
        base_dir = agents_path.parent

        # Markdown links
        for raw, line in self._extract_md_links(text):
            target = self._resolve_link(raw, base_dir)
            if target is None:
                continue
            if target.exists():
                self.verified_links += 1
            else:
                self.broken_links += 1
                self._add(
                    Finding(
                        "LINKS",
                        f"Broken markdown link: `{raw}` → `{target.relative_to(REPO_ROOT)}`",
                        line,
                    )
                )

        # Wikilinks
        for raw, line in self._extract_wikilinks(text):
            # Skip pure anchors like [[#section]]
            if raw.startswith("#"):
                continue
            target = self._resolve_link(raw, base_dir)
            if target is None:
                continue
            if target.exists():
                self.verified_links += 1
            else:
                self.broken_links += 1
                self._add(
                    Finding(
                        "LINKS",
                        f"Broken wikilink: `{raw}` → `{target.relative_to(REPO_ROOT)}`",
                        line,
                    )
                )

    # ------------------------------------------------------------------
    # 2. Count verification
    # ------------------------------------------------------------------
    def _count_glob(self, pattern: str) -> int:
        return len(list(REPO_ROOT.glob(pattern)))

    def _count_files(self, dir_rel: str) -> int:
        p = REPO_ROOT / dir_rel
        if not p.exists():
            return 0
        return len(
            [
                f
                for f in p.rglob("*")
                if (f.is_file() or f.is_symlink())
                and "__pycache__" not in f.parts
                and not f.name.endswith((".pyc", ".pyo"))
            ]
        )

    def _count_dirs(self, dir_rel: str) -> int:
        p = REPO_ROOT / dir_rel
        if not p.exists():
            return 0
        return len([d for d in p.iterdir() if d.is_dir()])

    def _git_ls_files(self, path_rel: str) -> int:
        code, out = self._run(["git", "ls-files", path_rel])
        if code != 0:
            return 0
        return len([ln for ln in out.splitlines() if ln.strip()])

    def _assert_count(
        self,
        label: str,
        expected: int,
        actual: int,
        line: int = 0,
        approximate: bool = False,
    ) -> None:
        if approximate:
            # Allow ±10% or ±3, whichever is larger
            tolerance = max(3, int(expected * 0.1))
            if abs(actual - expected) <= tolerance:
                return
            self._add(
                Finding(
                    "COUNTS",
                    f'Mismatch: "~{expected} {label}" (AGENTS.md line {line}) but found {actual}',
                    line,
                )
            )
        else:
            if expected != actual:
                self._add(
                    Finding(
                        "COUNTS",
                        f'"{expected} {label}" (AGENTS.md line {line}) but found {actual}',
                        line,
                    )
                )

    def check_counts(self) -> None:
        # Root: 62 test files  (line 111)
        actual = self._count_glob("tests/test_*.py")
        self._assert_count("test files", 62, actual, line=111)

        # Root: 71 Python modules in src/  (line 181)
        actual = len(list((REPO_ROOT / "src").rglob("*.py")))
        self._assert_count("src Python modules", 71, actual, line=181)

        # Root: 14 graph builder modules  (line 467)
        actual = self._count_glob("src/graph/builders/*.py")
        self._assert_count("graph builder modules", 14, actual, line=467)

        # Root: 18 index builder modules  (line 470)
        actual = self._count_glob("src/index/builders/*.py")
        self._assert_count("index builder modules", 18, actual, line=470)

        # Root: ~116 scripts + helpers  (line 115)
        actual = self._count_files("scripts")
        self._assert_count("scripts + helpers", 116, actual, line=115, approximate=True)

        # Root: ~72 tracked scripts  (line 183)
        actual = self._git_ls_files("scripts/")
        self._assert_count("tracked scripts", 72, actual, line=183, approximate=True)

        # Root: .claude/debug/ tracked files  (line 552 says 149+)
        actual = self._git_ls_files(".claude/debug/")
        self._assert_count("tracked debug files", 149, actual, line=552, approximate=True)

        # Profile counts
        # Profile: 22 gotcha files  (line 213)
        actual = self._count_files("profiles/pay-com/docs/gotchas")
        self._assert_count("gotcha files", 22, actual, line=213)

        # Profile: ~45 reference files + subdirs  (line 254)
        # Count top-level files + immediate subdirs in references/
        refs_dir = REPO_ROOT / "profiles/pay-com/docs/references"
        if refs_dir.exists():
            top_files = len([f for f in refs_dir.iterdir() if f.is_file()])
            top_dirs = len([d for d in refs_dir.iterdir() if d.is_dir()])
            actual_ref = top_files + top_dirs
        else:
            actual_ref = 0
        self._assert_count("reference files + subdirs", 45, actual_ref, line=254, approximate=True)

        # Profile: 20 flow files  (line 331)
        actual = self._count_files("profiles/pay-com/docs/flows")
        self._assert_count("flow files", 20, actual, line=331)

        # Profile: 10 MOC files  (line 358)
        actual = self._count_files("profiles/pay-com/docs/notes/_moc")
        self._assert_count("MOC files", 10, actual, line=358)

        # Profile: 52 provider folders  (line 375)
        actual = self._count_dirs("profiles/pay-com/docs/providers")
        self._assert_count("provider folders", 52, actual, line=375)

        # Profile: 25 profile scripts  (line 61, 448)
        actual = len(
            [f for f in (REPO_ROOT / "profiles/pay-com/scripts").iterdir() if f.is_file() and f.suffix == ".py"]
        )
        self._assert_count("profile scripts", 22, actual, line=448)

        # Profile: 4,087 docs files  (line 102)
        actual = self._count_files("profiles/pay-com/docs")
        self._assert_count("docs files", 4087, actual, line=102)

    # ------------------------------------------------------------------
    # 3. Orphan detection
    # ------------------------------------------------------------------
    def check_orphans(self) -> None:
        root_text = self._read(ROOT_AGENTS).lower()
        profile_text = self._read(PROFILE_AGENTS).lower()
        combined = root_text + "\n" + profile_text

        for entry in REPO_ROOT.iterdir():
            name = entry.name
            if any(fnmatch.fnmatch(name, pat) for pat in STANDARD_IGNORES):
                continue
            # Hidden files/dirs not in our allow-list are treated as standard ignores
            if (
                name.startswith(".")
                and name
                not in {
                    ".claude",
                    ".secrets",
                    ".gitignore",
                    ".pre-commit-config.yaml",
                    ".active_profile",
                    ".gitattributes",
                }
                and name not in combined
            ):
                continue
            # Check mention
            if name.lower() not in combined:
                self._add(
                    Finding(
                        "ORPHANS",
                        f"Not mentioned in AGENTS.md: {name}",
                        is_error=False,
                    )
                )

    # ------------------------------------------------------------------
    # 4. Storage classification spot-check
    # ------------------------------------------------------------------
    def _parse_storage_table(self, agents_path: Path) -> list[tuple[str, str, int]]:
        """Return list of (path, classification, line_no) from Storage Classification section."""
        lines = self._lines(agents_path)
        results: list[tuple[str, str, int]] = []
        in_section = False
        for line_no, line in enumerate(lines, 1):
            if re.match(r"^##\s+Storage\s+Classification", line, re.I):
                in_section = True
                continue
            if in_section and re.match(r"^##\s+", line):
                break
            if not in_section:
                continue
            # Table row: | path | classification | ... |
            parts = [p.strip() for p in line.split("|")]
            parts = [p for p in parts if p]
            if len(parts) >= 2:
                path_raw = parts[0]
                classification = parts[1].lower()
                # Skip header rows
                if "path" in path_raw.lower() and "classification" in classification:
                    continue
                # Strip backticks
                path_clean = path_raw.strip("`").strip()
                if path_clean and not path_clean.startswith("-"):
                    results.append((path_clean, classification, line_no))
        return results

    def check_storage(self) -> None:
        for agents_path in (ROOT_AGENTS, PROFILE_AGENTS):
            entries = self._parse_storage_table(agents_path)
            base_dir = agents_path.parent
            for path_raw, classification, line_no in entries:
                target = (base_dir / path_raw).resolve()
                # If the path has a wildcard or is not a concrete path, skip
                if "*" in path_raw or "…" in path_raw or "..." in path_raw:
                    continue
                # For directory rows that don't exist as exact paths, try repo-root relative
                if not target.exists():
                    alt = (REPO_ROOT / path_raw).resolve()
                    if alt.exists():
                        target = alt
                    else:
                        # Skip non-existent concrete paths (may be examples / patterns)
                        continue

                rel = target.relative_to(REPO_ROOT)

                if "gitignored" in classification and "git-tracked" not in classification:
                    code, _ = self._run(["git", "check-ignore", str(rel)])
                    if code != 0:
                        # Directory may not be ignored itself while all contents are
                        if target.is_dir():
                            ls_code, ls_out = self._run(["git", "ls-files", f"{rel}/"])
                            if ls_code == 0 and not ls_out.strip():
                                # No tracked files -> effectively gitignored, PASS
                                pass
                            else:
                                # There are tracked files, but check whether the
                                # directory is covered by .gitignore patterns by
                                # probing a non-existent path inside it.
                                probe_code, _ = self._run(
                                    [
                                        "git",
                                        "check-ignore",
                                        f"{rel}/.__health_probe__",
                                    ]
                                )
                                if probe_code != 0:
                                    # Not covered by gitignore -> real mismatch
                                    self._add(
                                        Finding(
                                            "STORAGE",
                                            f"`{rel}` classified as gitignored but is NOT ignored by git",
                                            line_no,
                                        )
                                    )
                                # else: covered by gitignore -> effectively gitignored, PASS
                        else:
                            self._add(
                                Finding(
                                    "STORAGE",
                                    f"`{rel}` classified as gitignored but is NOT ignored by git",
                                    line_no,
                                )
                            )
                elif "git" in classification and "gitignored" not in classification:
                    # git-tracked or git or git (ruff-excluded)
                    count = self._git_ls_files(str(rel))
                    if count == 0:
                        # Could be a directory — check if anything inside is tracked
                        if target.is_dir():
                            any_tracked = False
                            for child in target.rglob("*"):
                                if child.is_file():
                                    c = self._git_ls_files(str(child.relative_to(REPO_ROOT)))
                                    if c > 0:
                                        any_tracked = True
                                        break
                            if not any_tracked:
                                self._add(
                                    Finding(
                                        "STORAGE",
                                        f"`{rel}` classified as git-tracked but nothing in git index",
                                        line_no,
                                    )
                                )
                        else:
                            self._add(
                                Finding(
                                    "STORAGE",
                                    f"`{rel}` classified as git-tracked but not in git index",
                                    line_no,
                                )
                            )

    # ------------------------------------------------------------------
    # 5. Wikilink orphan check
    # ------------------------------------------------------------------
    def check_wikilink_orphans(self) -> None:
        for agents_path in (ROOT_AGENTS, PROFILE_AGENTS):
            text = self._read(agents_path)
            base_dir = agents_path.parent
            for raw, line in self._extract_wikilinks(text):
                if raw.startswith("#"):
                    continue  # anchor-only
                target = self._resolve_link(raw, base_dir)
                if target is None:
                    continue
                if not target.exists():
                    self.broken_links += 1
                    self._add(
                        Finding(
                            "LINKS",
                            f"Broken wikilink (orphan): `{raw}` → `{target.relative_to(REPO_ROOT)}`",
                            line,
                        )
                    )
                elif target.is_dir():
                    self.warn_links += 1
                    self._add(
                        Finding(
                            "LINKS",
                            f"Wikilink targets directory (should be .md): `{raw}` → `{target.relative_to(REPO_ROOT)}`",
                            line,
                            is_error=False,
                        )
                    )
                elif target.suffix.lower() != ".md":
                    self.warn_links += 1
                    self._add(
                        Finding(
                            "LINKS",
                            f"Wikilink does not target markdown: `{raw}` → `{target.relative_to(REPO_ROOT)}`",
                            line,
                            is_error=False,
                        )
                    )

    # ------------------------------------------------------------------
    # Report
    # ------------------------------------------------------------------
    def report(self) -> int:
        errors = [f for f in self.findings]
        warnings = [f for f in self.warnings]

        print("=== AGENTS.md Health Check ===\n")

        # LINKS
        print("[LINKS]")
        print(f"  ✅ Verified: {self.verified_links}")
        link_errors = [f for f in errors if f.category == "LINKS"]
        link_warns = [f for f in warnings if f.category == "LINKS"]
        print(f"  ❌ Broken: {len(link_errors)}")
        print(f"  ⚠️  Warnings: {len(link_warns)}")
        for f in link_errors + link_warns:
            prefix = "❌" if f.is_error else "⚠️"
            print(f"    {prefix} line {f.line}: {f.message}")
        print()

        # COUNTS
        print("[COUNTS]")
        count_errors = [f for f in errors if f.category == "COUNTS"]
        if not count_errors:
            print("  ✅ All counts match")
        else:
            for f in count_errors:
                print(f"  ❌ {f.message}")
        print()

        # ORPHANS
        print("[ORPHANS]")
        orphan_warns = [f for f in warnings if f.category == "ORPHANS"]
        if not orphan_warns:
            print("  ✅ No orphans detected")
        else:
            for f in orphan_warns:
                print(f"  ⚠️  {f.message}")
        print()

        # STORAGE
        print("[STORAGE]")
        storage_errors = [f for f in errors if f.category == "STORAGE"]
        if not storage_errors:
            print("  ✅ All classifications consistent")
        else:
            for f in storage_errors:
                print(f"  ❌ line {f.line}: {f.message}")
        print()

        # SUMMARY
        total_errors = len(errors)
        total_warns = len(warnings)
        print("[SUMMARY]")
        if total_errors == 0:
            print(f"PASS — {total_warns} warning(s) found")
            return 0
        else:
            print(f"FAIL — {total_errors} issue(s), {total_warns} warning(s) found")
            return 1


def main() -> int:
    checker = HealthChecker()

    checker.check_links(ROOT_AGENTS)
    checker.check_links(PROFILE_AGENTS)
    checker.check_wikilink_orphans()
    checker.check_counts()
    checker.check_orphans()
    checker.check_storage()

    return checker.report()


if __name__ == "__main__":
    sys.exit(main())
