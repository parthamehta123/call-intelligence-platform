"""Knowledge graph over the canonical product state.

The relational store answers the questions it was shaped for -- what is
wrong with this product, how many customers reported it. It answers
traversal questions badly, because every hop is another join:

  which other products show this failure mode?
  what else does firmware 7.2 affect?
  how is this issue connected to that one?

Those are the questions a support lead asks when deciding whether two
reports are one defect. The graph is built from the same Delta/SQLite
state rather than beside it, so there is one source of truth and the graph
is derived -- a second store to keep in step would be a second store to
get wrong.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import kb


@dataclass
class GraphStats:
    products: int = 0
    issues: int = 0
    versions: int = 0
    regions: int = 0
    edges: int = 0


def build():
    """Product -> issue -> version / region, as a directed graph."""
    import networkx as nx

    graph = nx.DiGraph()
    for row in kb.query("SELECT * FROM products"):
        graph.add_node(f"product:{row['product_id']}", kind="product",
                       name=row["canonical_name"], family=row["family"])

    import json
    for row in kb.query("SELECT * FROM issues"):
        product = f"product:{row['product_id']}"
        issue = f"issue:{row['product_id']}/{row['issue_key']}"
        graph.add_node(issue, kind="issue", key=row["issue_key"],
                       severity=row["severity"], status=row["status"],
                       customers=row["customers"], type=row["type"])
        graph.add_edge(product, issue, kind="has_issue")

        for version in json.loads(row["versions"] or "[]"):
            node = f"version:{row['product_id']}@{version}"
            graph.add_node(node, kind="version", value=version)
            graph.add_edge(issue, node, kind="affects_version")
            graph.add_edge(product, node, kind="ships_version")
        for region in json.loads(row["regions"] or "[]"):
            node = f"region:{region}"
            graph.add_node(node, kind="region", value=region)
            graph.add_edge(issue, node, kind="reported_in")
    return graph


def stats(graph=None) -> GraphStats:
    graph = graph if graph is not None else build()
    kinds = [d.get("kind") for _, d in graph.nodes(data=True)]
    return GraphStats(
        products=kinds.count("product"), issues=kinds.count("issue"),
        versions=kinds.count("version"), regions=kinds.count("region"),
        edges=graph.number_of_edges())


def shared_failure_modes(graph=None) -> list[tuple[str, list[str]]]:
    """Issue keys appearing on more than one product.

    The question behind it: is this one defect in a shared component, or
    several unrelated ones that happen to look alike? A relational schema
    can answer it with a self-join; the graph makes it a grouping.
    """
    graph = graph if graph is not None else build()
    by_key: dict[str, list[str]] = {}
    for node, data in graph.nodes(data=True):
        if data.get("kind") == "issue":
            product = node.split(":")[1].split("/")[0]
            by_key.setdefault(data["key"], []).append(product)
    return sorted(((key, sorted(set(products)))
                   for key, products in by_key.items() if len(set(products)) > 1),
                  key=lambda kv: -len(kv[1]))


def blast_radius(version: str, graph=None) -> list[str]:
    """Everything a version touches: the issues on it and their products."""
    graph = graph if graph is not None else build()
    hits: set[str] = set()
    for node, data in graph.nodes(data=True):
        if data.get("kind") == "version" and data.get("value") == version:
            hits.update(graph.predecessors(node))
    return sorted(hits)


def connection(issue_a: str, issue_b: str, graph=None) -> list[str]:
    """Shortest path between two issues, ignoring edge direction.

    This is the query the relational store genuinely cannot express
    without knowing the join path in advance -- the graph finds it.
    """
    import networkx as nx

    graph = graph if graph is not None else build()
    try:
        return nx.shortest_path(graph.to_undirected(), issue_a, issue_b)
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        return []


def render() -> str:
    graph = build()
    summary = stats(graph)
    lines = [
        "=== knowledge graph ===",
        f"  products {summary.products}  issues {summary.issues}  "
        f"versions {summary.versions}  regions {summary.regions}  "
        f"edges {summary.edges}",
        "",
        "  failure modes seen on more than one product:",
    ]
    shared = shared_failure_modes(graph)
    lines += [f"    {key:<22} {', '.join(products)}" for key, products in shared] \
        or ["    (none)"]

    issues = [n for n, d in graph.nodes(data=True) if d.get("kind") == "issue"]
    if len(issues) >= 2:
        path = connection(issues[0], issues[-1], graph)
        lines += ["", f"  how {issues[0]} connects to {issues[-1]}:"]
        lines += ["    " + " -> ".join(path)] if path else ["    (no path)"]

    versions = sorted({d["value"] for _, d in graph.nodes(data=True)
                       if d.get("kind") == "version"})
    if versions:
        lines += ["", f"  blast radius of version {versions[0]}:"]
        lines += [f"    {n}" for n in blast_radius(versions[0], graph)] or ["    (none)"]
    return "\n".join(lines)
