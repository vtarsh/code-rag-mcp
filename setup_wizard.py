#!/usr/bin/env python3
"""Interactive setup wizard for code-rag-mcp.

Creates a new profile, registers the MCP server, and optionally installs launchd auto-start.

Usage:
    python3 setup_wizard.py                          # Interactive mode
    python3 setup_wizard.py --org acme-corp          # Non-interactive with defaults
    python3 setup_wizard.py --org acme --model minilm --no-launchd
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).parent.resolve()
PROFILES_DIR = BASE_DIR / "profiles"

# Add project root for model registry
sys.path.insert(0, str(BASE_DIR))
os.environ.setdefault("CODE_RAG_HOME", str(BASE_DIR))
from src.models import DEFAULT_MODEL, EMBEDDING_MODELS  # noqa: E402


def prompt(msg: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    val = input(f"  {msg}{suffix}: ").strip()
    return val or default


def choose(msg: str, options: list[tuple[str, str]], default: str = "") -> str:
    print(f"\n  {msg}")
    for i, (key, desc) in enumerate(options, 1):
        marker = " (default)" if key == default else ""
        print(f"    [{i}] {key} — {desc}{marker}")
    while True:
        choice = input(f"  Choice [1-{len(options)}]: ").strip()
        if not choice and default:
            return default
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(options):
                return options[idx][0]
        except ValueError:
            pass
        print(f"    Please enter 1-{len(options)}")


def create_profile(name: str, org: str, npm_scope: str, model_key: str, display_name: str) -> Path:
    profile_dir = PROFILES_DIR / name
    profile_dir.mkdir(parents=True, exist_ok=True)

    config = {
        "org": org,
        "npm_scope": npm_scope,
        "grpc_domain_suffix": "",
        "server_name": "code-knowledge",
        "display_name": display_name,
        "embedding_model": model_key,
    }
    (profile_dir / "config.json").write_text(json.dumps(config, indent=2) + "\n")

    # Copy example YAML files
    example_dir = PROFILES_DIR / "example"
    for filename in ["glossary.yaml", "phrase_glossary.yaml", "known_flows.yaml"]:
        src = example_dir / filename
        dst = profile_dir / filename
        if src.exists() and not dst.exists():
            shutil.copy2(src, dst)

    # Create docs directories
    (profile_dir / "docs" / "flows").mkdir(parents=True, exist_ok=True)
    (profile_dir / "docs" / "gotchas").mkdir(parents=True, exist_ok=True)

    return profile_dir


def set_active_profile(name: str) -> None:
    (BASE_DIR / ".active_profile").write_text(name + "\n")


def register_claude_code(profile_name: str) -> bool:
    settings_path = Path.home() / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)

    settings = {}
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text())
        except json.JSONDecodeError:
            settings = {}

    settings.setdefault("mcpServers", {})
    settings["mcpServers"]["code-rag"] = {
        "command": "python3",
        "args": [str(BASE_DIR / "mcp_server.py")],
        "env": {
            "ACTIVE_PROFILE": profile_name,
            "CODE_RAG_HOME": str(BASE_DIR),
        },
    }

    settings_path.write_text(json.dumps(settings, indent=2) + "\n")
    return True


def install_launchd(profile_name: str) -> bool:
    plist_name = "com.code-rag.daemon.plist"
    plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.code-rag.daemon</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>{BASE_DIR}/daemon.py</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>ACTIVE_PROFILE</key>
        <string>{profile_name}</string>
        <key>CODE_RAG_HOME</key>
        <string>{BASE_DIR}</string>
        <key>HOME</key>
        <string>{Path.home()}</string>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>10</integer>
    <key>StandardOutPath</key>
    <string>/tmp/code-rag-daemon.out</string>
    <key>StandardErrorPath</key>
    <string>/tmp/code-rag-daemon.err</string>
</dict>
</plist>"""

    plist_dir = Path.home() / "Library" / "LaunchAgents"
    plist_path = plist_dir / plist_name

    if plist_path.exists():
        subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)

    plist_path.write_text(plist_content)
    result = subprocess.run(["launchctl", "load", str(plist_path)], capture_output=True)
    return result.returncode == 0


def check_prerequisites() -> list[str]:
    missing = []
    for cmd in ["git", "gh", "jq", "python3", "pip3"]:
        if not shutil.which(cmd):
            # pip3 can also be invoked via python3 -m pip
            if cmd == "pip3":
                try:
                    subprocess.run(
                        ["python3", "-m", "pip", "--version"],
                        capture_output=True,
                        check=True,
                    )
                    continue  # python3 -m pip works, skip
                except (subprocess.CalledProcessError, FileNotFoundError):
                    pass
            missing.append(cmd)
    return missing


def check_gh_auth() -> bool:
    result = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True)
    return result.returncode == 0


def parse_args():
    parser = argparse.ArgumentParser(description="code-rag-mcp setup wizard")
    parser.add_argument("--org", help="GitHub organization name (required in non-interactive mode)")
    parser.add_argument("--npm-scope", help="npm scope (default: @<org>)")
    parser.add_argument("--display-name", help="Display name (default: '<org> Knowledge Base')")
    parser.add_argument(
        "--model",
        choices=list(EMBEDDING_MODELS.keys()),
        default=DEFAULT_MODEL,
        help=f"Embedding model (default: {DEFAULT_MODEL})",
    )
    parser.add_argument("--no-register", action="store_true", help="Skip Claude Code MCP registration")
    parser.add_argument("--no-launchd", action="store_true", help="Skip launchd daemon auto-start")
    parser.add_argument("--skip-checks", action="store_true", help="Skip prerequisite checks")
    return parser.parse_args()


def main():
    args = parse_args()
    interactive = args.org is None

    print()
    print("=" * 50)
    print("  code-rag-mcp — Setup Wizard")
    print("=" * 50)
    print()

    # Check prerequisites
    if not args.skip_checks:
        print("Checking prerequisites...")
        missing = check_prerequisites()
        if missing:
            print(f"  Missing: {', '.join(missing)}")
            if "pip3" in missing:
                print("  Install pip: python3 -m ensurepip")
            brew_missing = [m for m in missing if m != "pip3"]
            if brew_missing:
                print("  Install with: brew install " + " ".join(brew_missing))
            sys.exit(1)
        print("  All prerequisites found.")

        if not check_gh_auth():
            print("\n  GitHub CLI not authenticated.")
            print("  Run: gh auth login")
            sys.exit(1)
        print("  GitHub CLI authenticated.")

    # Gather info
    if interactive:
        print("\nProfile setup:")
        org = prompt("GitHub org name")
        if not org:
            print("  Org name is required.")
            sys.exit(1)
        org_lower = org.lower()
        if org_lower != org:
            print(f"  Note: GitHub org/user names are lowercase, using '{org_lower}'")
            org = org_lower
        npm_scope = prompt("npm scope (optional)", f"@{org}")
        display_name = prompt("Display name", f"{org} Knowledge Base")
        download_sizes = {
            "coderank": "~230MB RAM. First download: ~250MB.",
            "minilm": "~80MB RAM. First download: ~90MB.",
        }
        model_options = [(key, f"{m.description} {download_sizes.get(key, '')}") for key, m in EMBEDDING_MODELS.items()]
        model_key = choose(
            "Embedding model (affects search quality and RAM, not build time):", model_options, default=DEFAULT_MODEL
        )
        print("  Note: Build time is the same regardless of model choice.")
    else:
        org = args.org.lower()
        npm_scope = args.npm_scope or f"@{org}"
        display_name = args.display_name or f"{org} Knowledge Base"
        model_key = args.model

    profile_name = org

    # Create profile
    print(f"\nCreating profile '{profile_name}'...")
    profile_dir = create_profile(profile_name, org, npm_scope, model_key, display_name)
    set_active_profile(profile_name)
    print(f"  Created: {profile_dir}/config.json")
    print(f"  Created: {profile_dir}/glossary.yaml")
    print(f"  Created: {profile_dir}/phrase_glossary.yaml")
    print(f"  Created: {profile_dir}/known_flows.yaml")
    print(f"  Active profile set to: {profile_name}")
    print(f"  Embedding model: {model_key} — {EMBEDDING_MODELS[model_key].description}")

    # Register MCP server
    do_register = True
    if interactive:
        do_register = prompt("\nRegister MCP server in Claude Code? [Y/n]", "Y").lower() != "n"
    elif args.no_register:
        do_register = False

    if do_register:
        if register_claude_code(profile_name):
            print("  Registered 'code-rag' in ~/.claude/settings.json")
        else:
            print("  Failed to register. Add manually to ~/.claude/settings.json")

    # Install launchd
    do_launchd = True
    if interactive:
        do_launchd = prompt("\nInstall daemon auto-start (launchd)? [Y/n]", "Y").lower() != "n"
    elif args.no_launchd:
        do_launchd = False

    if do_launchd:
        if install_launchd(profile_name):
            print("  Installed launchd plist")
        else:
            print("  Failed to install launchd plist")

    # Done
    print()
    print("=" * 50)
    print("  Setup complete!")
    print("=" * 50)
    print()
    print("Next steps:")
    print("  1. Build the index (clones repos, extracts, indexes — takes 30-60 min):")
    print(f"     make build PROFILE={profile_name}")
    print()
    print("  2. Verify everything works:")
    print(f"     make health PROFILE={profile_name}")
    print()
    print("  3. Start using in Claude Code (restart Claude Code after build):")
    print("     Search, trace dependencies, analyze tasks — all tools are ready!")
    print()
    print(f"  Tip: You chose {model_key}. To switch models later: make switch-model MODEL=minilm")
    print()
    print("Useful commands:")
    print(f"  make update PROFILE={profile_name}      # Incremental update")
    print("  make test                        # Run tests")
    print()
    print("Customize your profile:")
    print(f"  {profile_dir}/glossary.yaml          # Domain abbreviations")
    print(f"  {profile_dir}/phrase_glossary.yaml    # Multi-word expansions")
    print(f"  {profile_dir}/known_flows.yaml        # Business flow entry points")
    print(f"  {profile_dir}/docs/flows/             # Flow documentation (YAML)")
    print(f"  {profile_dir}/docs/gotchas/            # Gotchas & tips (Markdown)")
    print()


if __name__ == "__main__":
    main()
