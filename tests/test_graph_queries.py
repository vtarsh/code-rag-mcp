"""Tests for graph/queries.py — BFS and path-finding utilities."""

from src.graph.queries import bfs_chain


class TestBfsChain:
    def test_single_seed_no_neighbors(self):
        adj = {}
        result = bfs_chain(["A"], adj, depth_limit=3)
        assert result == {"A": (0, None, None)}

    def test_linear_chain(self):
        adj = {
            "A": [("B", "grpc_call", "")],
            "B": [("C", "webhook_dispatch", "")],
            "C": [("D", "child_workflow", "")],
        }
        result = bfs_chain(["A"], adj, depth_limit=3)
        assert result["A"] == (0, None, None)
        assert result["B"] == (1, "A", "grpc_call")
        assert result["C"] == (2, "B", "webhook_dispatch")
        assert result["D"] == (3, "C", "child_workflow")

    def test_depth_limit(self):
        adj = {
            "A": [("B", "grpc_call", "")],
            "B": [("C", "grpc_call", "")],
            "C": [("D", "grpc_call", "")],
        }
        result = bfs_chain(["A"], adj, depth_limit=2)
        assert "A" in result
        assert "B" in result
        assert "C" in result
        assert "D" not in result

    def test_skips_virtual_nodes(self):
        adj = {
            "A": [("pkg:lodash", "npm_dep", ""), ("B", "grpc_call", "")],
        }
        result = bfs_chain(["A"], adj, depth_limit=2)
        assert "pkg:lodash" not in result
        assert "B" in result

    def test_skips_all_virtual_prefixes(self):
        adj = {
            "A": [
                ("proto:Payment", "proto_import", ""),
                ("workflow:settlement", "child_workflow", ""),
                ("msg:PaymentRequest", "proto_message_def", ""),
                ("svc:PaymentService", "proto_service_def", ""),
            ],
        }
        result = bfs_chain(["A"], adj, depth_limit=2)
        assert len(result) == 1  # only A itself

    def test_multiple_seeds(self):
        adj = {
            "A": [("C", "grpc_call", "")],
            "B": [("C", "grpc_call", "")],
        }
        result = bfs_chain(["A", "B"], adj, depth_limit=2)
        assert result["A"] == (0, None, None)
        assert result["B"] == (0, None, None)
        assert "C" in result
        assert result["C"][0] == 1  # depth 1

    def test_cycle_handling(self):
        adj = {
            "A": [("B", "grpc_call", "")],
            "B": [("A", "grpc_call", "")],
        }
        result = bfs_chain(["A"], adj, depth_limit=5)
        # Should not loop infinitely, visit each node once
        assert len(result) == 2
        assert result["A"] == (0, None, None)
        assert result["B"] == (1, "A", "grpc_call")

    def test_diamond_graph(self):
        adj = {
            "A": [("B", "grpc_call", ""), ("C", "webhook_dispatch", "")],
            "B": [("D", "grpc_call", "")],
            "C": [("D", "child_workflow", "")],
        }
        result = bfs_chain(["A"], adj, depth_limit=3)
        assert result["D"][0] == 2  # reached at depth 2
