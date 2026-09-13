"""knowledge.py — parse the agent's world-model text files into JSON the UI can
render.

Two inputs, both read verbatim from `chromapilot/Knowledge/` (the SAME strings the
graph receives as node_context / graph_context):

  * node.txt  — a numbered, plain-text tool registry. We extract each tool's
                name, function summary, input parameters, outputs and path
                convention.
  * graph.txt — the dependency DAG, written as "A -> B" arrows grouped by
                protocol (Hiplex Cut&Tag, ChIP-DIP). We extract the edges.

The parsing is intentionally tolerant: if the text format drifts, we degrade to
showing less rather than crashing. In particular blank lines between fields
(which node.txt now uses) must never break a field boundary.
"""

from __future__ import annotations

import re

FIELDS = ("Input", "Output", "Function", "Path Convention")
_FIELD_RE = re.compile(
    r"^[ \t]{0,6}-[ \t]*(Input|Output|Function|Path Convention)\b[^:\n]*:?[ \t]*(.*)$",
    re.IGNORECASE,
)
_NODE_RE = re.compile(r"(?m)^(\d+)\.\s+([a-zA-Z_][\w]*)[ \t]*$")
_PARAM_RE = re.compile(r"^\s*(\d+)\.\s*<([^>]+)>\s*(.*)$")


def parse_nodes(node_txt: str) -> list[dict]:
    """Return one dict per tool:
    {index, name, function, summary, note, inputs, outputs, path_convention,
     params: [{name, desc}], per_sample}
    """
    tools: list[dict] = []
    if not node_txt:
        return tools

    # Work only within the "Node Descriptions" section if present.
    marker = node_txt.find("Node Descriptions")
    body = node_txt[marker:] if marker >= 0 else node_txt

    matches = list(_NODE_RE.finditer(body))
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        block = body[start:end]
        fields, note = _split_fields(block)
        function = _paragraphs(fields.get("function", ""))
        inputs = _dedent(fields.get("input", ""))
        params = _parse_params(inputs)
        tools.append({
            "index": int(m.group(1)),
            "name": m.group(2),
            "function": function or _first_sentence(block),
            "summary": _summary(function or _first_sentence(block)),
            "note": _squash(note),
            "inputs": inputs,
            "outputs": _dedent(fields.get("output", "")),
            "path_convention": _dedent(fields.get("path convention", "")),
            "params": params,
            "per_sample": any(p["name"] == "sample" for p in params),
        })
    return tools


def _split_fields(block: str) -> tuple[dict, str]:
    """Split a tool block into its top-level '- Field:' sections. Returns
    ({field_lower: text}, preamble_text). Blank lines never end a section."""
    fields: dict[str, list[str]] = {}
    preamble: list[str] = []
    current: str | None = None
    for raw in block.splitlines():
        m = _FIELD_RE.match(raw)
        if m:
            current = m.group(1).lower()
            fields[current] = []
            rest = m.group(2).strip()
            if rest:
                fields[current].append(rest)
            continue
        if current is None:
            preamble.append(raw)
        else:
            fields[current].append(raw.rstrip())
    out = {k: "\n".join(v).strip("\n") for k, v in fields.items()}
    return out, "\n".join(x for x in preamble if x.strip()).strip()


def _parse_params(inputs: str) -> list[dict]:
    """Turn the numbered '<param>' list of an Input section into
    [{name, desc}]. Description lines are joined; sub-bullets keep their own
    line so the UI can show them as a small list."""
    params: list[dict] = []
    cur: dict | None = None
    for raw in inputs.splitlines():
        m = _PARAM_RE.match(raw)
        if m:
            cur = {"name": m.group(2).strip(), "desc_lines": []}
            if m.group(3).strip():
                cur["desc_lines"].append(m.group(3).strip())
            params.append(cur)
            continue
        if cur is None or not raw.strip():
            continue
        line = raw.strip()
        if line.startswith("-"):
            cur["desc_lines"].append("\n" + line)
        else:
            cur["desc_lines"].append(line)
    for p in params:
        text = " ".join(p.pop("desc_lines")).replace(" \n", "\n").strip()
        p["desc"] = re.sub(r"[ \t]+", " ", text)
    return params


def _dedent(text: str) -> str:
    lines = [ln.rstrip() for ln in text.splitlines()]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    indents = [len(ln) - len(ln.lstrip()) for ln in lines if ln.strip()]
    cut = min(indents) if indents else 0
    return "\n".join(ln[cut:] if ln.strip() else "" for ln in lines)


def _paragraphs(text: str) -> str:
    """Collapse hard-wrapped lines into paragraphs (blank line = new paragraph),
    normalising internal whitespace. Markdown bold markers are dropped."""
    paras: list[str] = []
    buf: list[str] = []
    for ln in text.splitlines():
        if ln.strip():
            buf.append(ln.strip())
        elif buf:
            paras.append(" ".join(buf))
            buf = []
    if buf:
        paras.append(" ".join(buf))
    return "\n\n".join(p.replace("**", "") for p in paras).strip()


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _summary(function: str, limit: int = 230) -> str:
    """First sentence (or two) of the function text, for compact listings."""
    first = function.split("\n\n", 1)[0]
    m = re.match(r"^(.{20,}?[.!?])(\s|$)", first)
    s = m.group(1) if m else first
    if len(s) > limit:
        s = s[:limit].rsplit(" ", 1)[0] + "…"
    return s.strip()


def _first_sentence(block: str) -> str:
    text = " ".join(block.split())
    return (text[:160] + "…") if len(text) > 160 else text


# --------------------------------------------------------------------------- #
# Dependency DAG
# --------------------------------------------------------------------------- #
def parse_graph(graph_txt: str) -> dict:
    """Return {nodes: [...], edges: [{source, target, protocol}], protocols: [...]}."""
    edges: list[dict] = []
    nodes: set[str] = set()
    protocols: list[str] = []
    current = "pipeline"

    for raw in (graph_txt or "").splitlines():
        line = raw.strip()
        if not line or set(line) <= {"=", "-"}:
            continue
        # A short title line with no arrow starts a new protocol section.
        if "->" not in line and "→" not in line:
            if line and not line.lower().startswith("dependency"):
                current = line
                if current not in protocols:
                    protocols.append(current)
            continue
        # Normalise both ASCII "->" and the unicode arrow.
        chain = re.split(r"\s*(?:->|→)\s*", line.lstrip("-• ").strip())
        chain = [c.strip() for c in chain if c.strip()]
        for a, b in zip(chain, chain[1:]):
            nodes.add(a)
            nodes.add(b)
            edge = {"source": a, "target": b, "protocol": current}
            if edge not in edges:
                edges.append(edge)

    return {"nodes": sorted(nodes), "edges": edges, "protocols": protocols}


def layered_layout(graph: dict) -> dict:
    """Assign each node a layer (longest path from a root) and a row so the UI
    can draw a left-to-right DAG. Rows are ordered by the barycentre of the
    predecessors' rows (roots: longest downstream chain first) so that long
    edges run beside — not through — the main preprocessing chain.
    Returns {name: {layer, row}}."""
    nodes = list(graph["nodes"])
    edges = graph["edges"]
    preds: dict[str, list[str]] = {n: [] for n in nodes}
    succ: dict[str, list[str]] = {n: [] for n in nodes}
    for e in edges:
        preds.setdefault(e["target"], []).append(e["source"])
        succ.setdefault(e["source"], []).append(e["target"])

    layer = {n: 0 for n in nodes}
    for _ in range(len(nodes) + 1):        # DAG: bounded relaxation converges
        changed = False
        for e in edges:
            if layer[e["target"]] < layer[e["source"]] + 1:
                layer[e["target"]] = layer[e["source"]] + 1
                changed = True
        if not changed:
            break

    # Number of nodes reachable downstream — used to order the roots.
    def reach(n, seen=None):
        seen = seen if seen is not None else set()
        for s in succ.get(n, []):
            if s not in seen:
                seen.add(s)
                reach(s, seen)
        return len(seen)

    pos: dict[str, dict] = {}
    max_layer = max(layer.values()) if layer else 0
    for L in range(max_layer + 1):
        members = [n for n in nodes if layer[n] == L]
        if L == 0:
            members.sort(key=lambda n: (-reach(n), n))
        else:
            def bary(n):
                rows = [pos[p]["row"] for p in preds.get(n, []) if p in pos]
                return (sum(rows) / len(rows)) if rows else 0
            members.sort(key=lambda n: (bary(n), n))
        for r, n in enumerate(members):
            pos[n] = {"layer": L, "row": r}
    return pos
