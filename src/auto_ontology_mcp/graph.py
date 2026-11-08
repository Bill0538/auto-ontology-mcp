"""Join paths over accepted relationships."""

from __future__ import annotations

from collections import deque
from typing import Any



def _label(schema: str | None, table: str) -> str:
    return f"{schema}.{table}" if schema else table


def shared_parents(hops: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Tables that are the one side of two or more hops.

    Their many sides are related only through that table: order_lines → products ← product_tags
    pairs every order line with every tag of the same product, which says nothing about which line
    belongs to which tag. Joining such siblings in one SELECT also multiplies their rows.
    """
    children: dict[str, list[str]] = {}
    for hop in hops:
        if hop["multiplicity"] == "many-to-one":
            one, many = hop["to"], hop["from"]
        elif hop["multiplicity"] == "one-to-many":
            one, many = hop["from"], hop["to"]
        else:
            continue
        if many not in children.setdefault(one, []):
            children[one].append(many)
    return [{"parent": p, "children": c} for p, c in children.items() if len(c) >= 2]


class JoinGraph:
    def __init__(self, relationships: list[dict[str, Any]], tables: list[dict[str, Any]]) -> None:
        self.tables = {t["id"]: t for t in tables}
        self.adj: dict[str, list[tuple[str, dict[str, Any], str]]] = {t["id"]: [] for t in tables}
        for r in relationships:
            if r["status"] != "accepted":
                continue
            s, t = r["source_table_id"], r["target_table_id"]
            if s == t:
                continue  # self references are not join hops between tables
            self.adj.setdefault(s, []).append((t, r, "forward"))
            self.adj.setdefault(t, []).append((s, r, "reverse"))

    def _edges(self, node: str) -> list[tuple[str, dict[str, Any], str]]:
        # Default joins first so BFS prefers them among equal-length paths.
        return sorted(self.adj.get(node, []), key=lambda e: (0 if e[1].get("is_default") else 1, e[1]["source_column"]))

    def path(self, start: str, goal: str, max_hops: int = 6) -> list[dict[str, Any]] | None:
        if start == goal:
            return []
        prev: dict[str, tuple[str, dict[str, Any], str]] = {}
        seen = {start}
        queue = deque([(start, 0)])
        while queue:
            node, depth = queue.popleft()
            if depth >= max_hops:
                continue
            for nxt, rel, direction in self._edges(node):
                if nxt in seen:
                    continue
                seen.add(nxt)
                prev[nxt] = (node, rel, direction)
                if nxt == goal:
                    hops = []
                    cur = goal
                    while cur != start:
                        p, r, d = prev[cur]
                        hops.append(self._hop(p, cur, r, d))
                        cur = p
                    return list(reversed(hops))
                queue.append((nxt, depth + 1))
        return None

    def alternatives(self, a: str, b: str) -> list[dict[str, Any]]:
        """Other direct relationships between two tables (e.g. billing vs delivery city)."""
        out = []
        for nxt, rel, direction in self.adj.get(a, []):
            if nxt == b:
                out.append(self._hop(a, b, rel, direction))
        return out

    def _hop(self, frm: str, to: str, rel: dict[str, Any], direction: str) -> dict[str, Any]:
        ft, tt = self.tables[frm], self.tables[to]
        if direction == "forward":
            on = f"{ft['name']}.{rel['source_column']} = {tt['name']}.{rel['target_column']}"
            multiplicity = "many-to-one" if rel.get("cardinality") != "one_to_one" else "one-to-one"
            fanout = False
        else:
            on = f"{ft['name']}.{rel['target_column']} = {tt['name']}.{rel['source_column']}"
            multiplicity = "one-to-many" if rel.get("cardinality") != "one_to_one" else "one-to-one"
            fanout = rel.get("cardinality") != "one_to_one"
        return {
            "from": _label(ft.get("schema_name"), ft["name"]),
            "to": _label(tt.get("schema_name"), tt["name"]),
            "on": on,
            "multiplicity": multiplicity,
            "fans_out": fanout,
            "relationship_id": rel["id"],
            "role": rel.get("role"),
            "is_default": bool(rel.get("is_default")),
        }

    def connect(self, table_ids: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
        """Greedy Steiner tree: join every table to the growing set by its shortest path."""
        if not table_ids:
            return [], []
        tree = {table_ids[0]}
        hops: list[dict[str, Any]] = []
        unreachable = []
        for tid in table_ids[1:]:
            if tid in tree:
                continue
            best = None
            for anchor in tree:
                p = self.path(anchor, tid)
                if p is not None and (best is None or len(p) < len(best)):
                    best = p
            if best is None:
                unreachable.append(tid)
                continue
            for hop in best:
                if hop not in hops:
                    hops.append(hop)
            for hop in best:
                for t in (hop["from"], hop["to"]):
                    for k, v in self.tables.items():
                        if _label(v.get("schema_name"), v["name"]) == t:
                            tree.add(k)
        return hops, unreachable

