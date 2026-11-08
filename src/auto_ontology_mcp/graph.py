"""Join paths over accepted relationships."""

from __future__ import annotations

from collections import deque
from typing import Any



def _label(schema: str | None, table: str) -> str:
    return f"{schema}.{table}" if schema else table


def shared_parents(hops: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Tables that two or more hops reference (their key is the target of the foreign key).

    The referencing tables are related only through that table: order_lines → products ←
    product_tags pairs every order line with every tag of the same product, which says nothing
    about which line belongs to which tag. Joining such siblings in one SELECT also multiplies
    their rows. The foreign key direction decides, not the cardinality: a one-to-one cardinality
    inferred from sampled data (one child row per parent so far) does not make two tables linked.
    """
    children: dict[str, list[str]] = {}
    for hop in hops:
        parent = hop.get("referenced")
        if parent is None:  # hops built before the field existed
            parent = {"many-to-one": hop["to"], "one-to-many": hop["from"]}.get(hop["multiplicity"])
            if parent is None:
                continue
        child = hop["from"] if parent == hop["to"] else hop["to"]
        if child not in children.setdefault(parent, []):
            children[parent].append(child)
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

    def linked_path(self, start: str, goal: str, max_hops: int = 6) -> list[dict[str, Any]] | None:
        """Shortest path that never goes up to a table and straight back down from it.

        a → parent ← b only says that a and b share a parent; it does not link a row of a to a row
        of b. When the shortest path has that shape, a path that only climbs from a row to the rows
        it references (an order line → its order → the order's customer), or comes down and climbs
        again through a bridge table, is the real link even if it is longer. None when no such path
        exists.
        """
        if start == goal:
            return []
        # A state is (table, came_up): after following a foreign key to the table it references,
        # the next hop may not go down to another table that references it.
        begin = (start, False)
        prev: dict[tuple[str, bool], tuple[tuple[str, bool], dict[str, Any], str]] = {}
        seen = {begin}
        queue = deque([(begin, 0)])
        while queue:
            state, depth = queue.popleft()
            if depth >= max_hops:
                continue
            node, came_up = state
            for nxt, rel, direction in self._edges(node):
                # Direction, not cardinality: a one-to-one inferred from sampled data still only shares the table.
                if came_up and direction == "reverse":
                    continue  # up then down: the two sides only share this table
                nstate = (nxt, direction == "forward")
                if nstate in seen or any(s[0] == nxt for s in self._trail(prev, state, begin)):
                    continue
                seen.add(nstate)
                prev[nstate] = (state, rel, direction)
                if nxt == goal:
                    hops = []
                    cur = nstate
                    while cur != begin:
                        p, r, d = prev[cur]
                        hops.append(self._hop(p[0], cur[0], r, d))
                        cur = p
                    return list(reversed(hops))
                queue.append((nstate, depth + 1))
        return None

    @staticmethod
    def _trail(prev: dict, state: tuple[str, bool], begin: tuple[str, bool]) -> list[tuple[str, bool]]:
        """States on the way to *state*, so a path never visits a table twice."""
        out = [state]
        while state != begin:
            state = prev[state][0]
            out.append(state)
        return out

    def best_path(self, start: str, goal: str, max_hops: int = 6) -> list[dict[str, Any]] | None:
        """The linked path when there is one, otherwise the shortest path."""
        linked = self.linked_path(start, goal, max_hops)
        return linked if linked is not None else self.path(start, goal, max_hops)

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
        frm_label, to_label = _label(ft.get("schema_name"), ft["name"]), _label(tt.get("schema_name"), tt["name"])
        return {
            "from": frm_label,
            "to": to_label,
            "on": on,
            "multiplicity": multiplicity,
            "fans_out": fanout,
            "referenced": to_label if direction == "forward" else frm_label,
            "relationship_id": rel["id"],
            "role": rel.get("role"),
            "is_default": bool(rel.get("is_default")),
        }

    def connect(self, table_ids: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
        """Greedy Steiner tree: join every table to the growing set by its shortest linked path
        (see linked_path), or by its shortest path when no linked one exists."""
        if not table_ids:
            return [], []
        tree = {table_ids[0]}
        hops: list[dict[str, Any]] = []
        unreachable = []
        for tid in table_ids[1:]:
            if tid in tree:
                continue
            best = None
            for find in (self.linked_path, self.path):
                for anchor in tree:
                    p = find(anchor, tid)
                    if p is not None and (best is None or len(p) < len(best)):
                        best = p
                if best is not None:
                    break  # a linked path from any anchor beats a shorter one through a shared parent
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

