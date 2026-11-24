"""Left-to-right layered layout for 'show me' diagrams. Pure Python: the overlay (system
Python) and the daemon's browser page (venv) both import it, so they draw the same picture."""


def layered_layout(nodes: list[dict], edges: list[dict]) -> dict[str, tuple[float, float]]:
    """{node id: (x, y)} in 0..1 space. Columns are dependency depth (longest path from a
    source); within a column, nodes sit near the average height of their parents."""
    ids = [n["id"] for n in nodes]
    succ = {i: [] for i in ids}
    preds = {i: [] for i in ids}
    for e in edges:
        a, b = e.get("from"), e.get("to")
        if a in succ and b in succ and a != b:
            succ[a].append(b)
            preds[b].append(a)

    # Longest-path depth over a DAG (Kahn); anything stuck in a cycle goes after its deepest parent.
    indeg = {i: len(preds[i]) for i in ids}
    depth = {i: 0 for i in ids}
    queue = [i for i in ids if indeg[i] == 0]
    seen = set()
    while queue:
        node = queue.pop(0)
        seen.add(node)
        for nxt in succ[node]:
            depth[nxt] = max(depth[nxt], depth[node] + 1)
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                queue.append(nxt)
    # Nodes on a cycle never reach in-degree 0: walk them in the order given, each one
    # placed after whichever of its parents is already placed (a back edge is ignored).
    for node in ids:
        if node not in seen:
            known = [depth[p] for p in preds[node] if p in seen]
            depth[node] = (max(known) + 1) if known else 0
            seen.add(node)

    columns: dict[int, list[str]] = {}
    for node in ids:
        columns.setdefault(depth[node], []).append(node)
    levels = sorted(columns)
    pos: dict[str, tuple[float, float]] = {}
    for li, level in enumerate(levels):
        column = columns[level]
        if li:
            def bary(n):
                ys = [pos[p][1] for p in preds[n] if p in pos]
                return sum(ys) / len(ys) if ys else .5
            column.sort(key=bary)
        for k, node in enumerate(column):
            pos[node] = ((li + .5) / len(levels), (k + .5) / len(column))
    return pos
