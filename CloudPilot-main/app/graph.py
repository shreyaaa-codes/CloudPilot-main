"""Dependency graph traversal over an explicitly selected topology source."""
from __future__ import annotations

import json
from collections import deque
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol

TOPOLOGY_PATH = Path(__file__).parent.parent / "topology" / "default.json"


class TopologySource(Protocol):
    """Supplies one topology snapshot without exposing its storage mechanism."""
    name: str

    def topology(self) -> dict[str, list[dict[str, Any]]]: ...


class TopologySourceRequiredError(RuntimeError):
    """Raised when analysis would otherwise silently omit blast-radius data."""


class FixtureTopologySource:
    """Deterministic JSON topology intended only for unit tests and local fixtures."""
    name = "fixture"

    def __init__(self, topology: dict[str, list[dict[str, Any]]] | None = None):
        self._topology = topology if topology is not None else load_fixture_topology()

    def topology(self) -> dict[str, list[dict[str, Any]]]:
        return self._topology


def load_fixture_topology() -> dict[str, list[dict[str, Any]]]:
    """Load the version-controlled fixture; production analysis must not call this."""
    with TOPOLOGY_PATH.open() as file:
        return json.load(file)


# Kept as a compatibility alias for existing fixture-oriented callers.
def load_topology() -> dict[str, list[dict[str, Any]]]:
    return load_fixture_topology()


def adjacency(topology: dict[str, list[dict[str, Any]]]) -> dict[str, list[str]]:
    graph = {node["id"]: [] for node in topology["nodes"]}
    for edge in topology["edges"]:
        graph.setdefault(edge["source"], []).append(edge["target"])
    return graph


def bfs(graph: dict[str, list[str]], start: str, max_depth: int = 3) -> dict[str, int]:
    """Return every downstream node, mapped to its shortest-hop distance."""
    distances, queue = {}, deque([(start, 0)])
    visited = {start}
    while queue:
        node, depth = queue.popleft()
        if depth >= max_depth:
            continue
        for neighbor in graph.get(node, []):
            if neighbor not in visited:
                visited.add(neighbor)
                distances[neighbor] = depth + 1
                queue.append((neighbor, depth + 1))
    return distances


def dfs_paths(graph: dict[str, list[str]], start: str, max_depth: int = 3) -> list[list[str]]:
    """Enumerate downstream paths for the explanation panel; depth bounded for safety."""
    paths: list[list[str]] = []

    def walk(node: str, path: list[str]):
        if len(path) - 1 == max_depth or not graph.get(node):
            if len(path) > 1:
                paths.append(path)
            return
        for neighbor in graph[node]:
            if neighbor not in path:
                walk(neighbor, path + [neighbor])

    walk(start, [start])
    return paths


def blast_radius(
    changed_addresses: list[str],
    max_depth: int = 3,
    topology_source: TopologySource | None = None,
    tier1_service_ids_by_address: Mapping[str, Iterable[str]] | None = None,
) -> dict[str, Any]:
    """Calculate graph impact and Tier-1 coverage from explicit business context."""
    if topology_source is None:
        raise TopologySourceRequiredError("A topology source is required to calculate blast radius.")
    source = topology_source
    topology = source.topology()
    graph = adjacency(topology)
    nodes = {node["id"]: node for node in topology["nodes"]}
    impacted: dict[str, int] = {}
    paths: list[list[str]] = []
    for address in changed_addresses:
        if address in nodes:
            for node, depth in bfs(graph, address, max_depth).items():
                impacted[node] = min(depth, impacted.get(node, depth))
            paths.extend(dfs_paths(graph, address, max_depth))
    impacted_nodes = [{**nodes[node], "depth": depth} for node, depth in sorted(impacted.items(), key=lambda item: (item[1], item[0]))]
    production = sum(node.get("environment") == "production" for node in impacted_nodes)
    affected_addresses = set(changed_addresses) | set(impacted)
    tier1_service_ids = {
        str(service_id)
        for address in affected_addresses
        for service_id in (tier1_service_ids_by_address or {}).get(address, [])
        if isinstance(service_id, (str, int))
    }
    return {"changed_nodes": [item for item in changed_addresses if item in nodes], "impacted_nodes": impacted_nodes,
            "paths": paths, "direct_dependencies": sum(depth == 1 for depth in impacted.values()),
            "indirect_dependencies": sum(depth > 1 for depth in impacted.values()), "production_services": production,
            "tier1_services": len(tier1_service_ids), "max_depth": max(impacted.values(), default=0), "topology_source": source.name}
