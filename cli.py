#!/usr/bin/env python3
"""CLI for pay-knowledge RAG — enables sub-agents to query RAG via Bash.

Usage:
  python3 ~/.code-rag/cli.py search "trustly webhook verification"
  python3 ~/.code-rag/cli.py search "uniqueIdentifier lifecycle" --repo grpc-apm-trustly
  python3 ~/.code-rag/cli.py search "mandate" --file-type gotchas
  python3 ~/.code-rag/cli.py analyze-task "add verification to Trustly" --provider trustly
  python3 ~/.code-rag/cli.py context "trustly verification webhook handling"
  python3 ~/.code-rag/cli.py find-deps grpc-apm-trustly
  python3 ~/.code-rag/cli.py trace-impact grpc-apm-trustly
  python3 ~/.code-rag/cli.py trace-flow express-api-internal next-web-pay-with-bank
  python3 ~/.code-rag/cli.py trace-chain payment --direction downstream --depth 3
  python3 ~/.code-rag/cli.py repo-overview grpc-apm-trustly
"""

import json
import sys
import urllib.request

DAEMON_URL = "http://localhost:8742/tool"


def call_tool(tool_name: str, args: dict) -> str:
    """Call RAG daemon tool and return result text."""
    url = f"{DAEMON_URL}/{tool_name}"
    data = json.dumps(args).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
            return result.get("result", json.dumps(result, indent=2))
    except urllib.error.URLError as e:
        return f"ERROR: Cannot connect to RAG daemon at {DAEMON_URL}. Is it running?\n{e}"
    except Exception as e:
        return f"ERROR: {e}"


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "search":
        if len(sys.argv) < 3:
            print("Usage: cli.py search <query> [--repo NAME] [--file-type TYPE] [--limit N]")
            sys.exit(1)
        query = sys.argv[2]
        args = {"query": query}
        i = 3
        while i < len(sys.argv):
            if sys.argv[i] == "--repo" and i + 1 < len(sys.argv):
                args["repo"] = sys.argv[i + 1]
                i += 2
            elif sys.argv[i] == "--file-type" and i + 1 < len(sys.argv):
                args["file_type"] = sys.argv[i + 1]
                i += 2
            elif sys.argv[i] == "--limit" and i + 1 < len(sys.argv):
                args["limit"] = int(sys.argv[i + 1])
                i += 2
            else:
                i += 1
        print(call_tool("search", args))

    elif cmd == "analyze-task":
        if len(sys.argv) < 3:
            print("Usage: cli.py analyze-task <description> [--provider NAME]")
            sys.exit(1)
        args = {"description": sys.argv[2]}
        if "--provider" in sys.argv:
            idx = sys.argv.index("--provider")
            if idx + 1 < len(sys.argv):
                args["provider"] = sys.argv[idx + 1]
        print(call_tool("analyze_task", args))

    elif cmd == "context":
        if len(sys.argv) < 3:
            print("Usage: cli.py context <query> [--repo NAME]")
            sys.exit(1)
        args = {"query": sys.argv[2]}
        if "--repo" in sys.argv:
            idx = sys.argv.index("--repo")
            if idx + 1 < len(sys.argv):
                args["repo"] = sys.argv[idx + 1]
        print(call_tool("context_builder", args))

    elif cmd == "find-deps":
        if len(sys.argv) < 3:
            print("Usage: cli.py find-deps <repo_name>")
            sys.exit(1)
        print(call_tool("find_dependencies", {"repo_name": sys.argv[2]}))

    elif cmd == "trace-impact":
        if len(sys.argv) < 3:
            print("Usage: cli.py trace-impact <repo_name> [--depth N]")
            sys.exit(1)
        args = {"repo_name": sys.argv[2]}
        if "--depth" in sys.argv:
            idx = sys.argv.index("--depth")
            if idx + 1 < len(sys.argv):
                args["depth"] = int(sys.argv[idx + 1])
        print(call_tool("trace_impact", args))

    elif cmd == "trace-flow":
        if len(sys.argv) < 4:
            print("Usage: cli.py trace-flow <source> <target> [--depth N]")
            sys.exit(1)
        args = {"source": sys.argv[2], "target": sys.argv[3]}
        if "--depth" in sys.argv:
            idx = sys.argv.index("--depth")
            if idx + 1 < len(sys.argv):
                args["max_depth"] = int(sys.argv[idx + 1])
        print(call_tool("trace_flow", args))

    elif cmd == "trace-chain":
        if len(sys.argv) < 3:
            print("Usage: cli.py trace-chain <repo_or_concept> [--direction downstream|upstream|both] [--depth N]")
            sys.exit(1)
        args = {"start": sys.argv[2]}
        if "--direction" in sys.argv:
            idx = sys.argv.index("--direction")
            if idx + 1 < len(sys.argv):
                args["direction"] = sys.argv[idx + 1]
        if "--depth" in sys.argv:
            idx = sys.argv.index("--depth")
            if idx + 1 < len(sys.argv):
                args["max_depth"] = int(sys.argv[idx + 1])
        print(call_tool("trace_chain", args))

    elif cmd == "repo-overview":
        if len(sys.argv) < 3:
            print("Usage: cli.py repo-overview <repo_name>")
            sys.exit(1)
        print(call_tool("repo_overview", {"repo_name": sys.argv[2]}))

    elif cmd == "gotchas":
        # Shortcut: search only gotchas chunks
        query = sys.argv[2] if len(sys.argv) > 2 else ""
        if not query:
            print("Usage: cli.py gotchas <query>")
            sys.exit(1)
        print(call_tool("search", {"query": query, "file_type": "gotchas", "limit": 10}))

    elif cmd == "health":
        print(call_tool("health_check", {}))

    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
