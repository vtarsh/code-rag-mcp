#!/usr/bin/env python3
"""
Build dependency graph from extracted artifacts.

Parses:
  1. ENV configs — *_GRPC_URL vars → service-to-service gRPC calls
  2. Scoped npm deps — package.json dependencies → npm package edges
  3. Proto imports — proto file imports → proto dependency edges
  4. K8s configs — service names, env vars pointing to other services

Output: graph_nodes + graph_edges tables in knowledge.db

Thin entry point — implementation lives in `src/graph/builders/`.
"""

import sys
from pathlib import Path

# Ensure repo root is on sys.path so `src.graph.builders` resolves
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.graph.builders import build_graph

if __name__ == "__main__":
    build_graph()
