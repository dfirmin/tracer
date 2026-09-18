"""Check citations and graph invariants, without parsing source code."""
from pathlib import Path
from urllib.parse import quote

from .contracts import DISCOVERY, TRACE, REVIEW, validate


def check_evidence(items, snapshot, required=True):
    if required and not items:
        raise ValueError("Missing evidence")
    base = Path(snapshot).resolve()
    for item in items:
        path = Path(item["path"])
        file = base / path
        if path.is_absolute() or ".." in path.parts or not file.resolve().is_relative_to(base):
            raise ValueError("Citation escapes snapshot")
        lines = file.read_text(encoding="utf-8").splitlines()
        first, last = item["start_line"], item["end_line"]
        if not 1 <= first <= last <= len(lines):
            raise ValueError(f"Invalid citation range: {path}:{first}-{last}")
        if not item["quote"].strip() or item["quote"] not in "\n".join(lines[first-1:last]):
            raise ValueError(f"Quote does not occur at citation: {path}:{first}-{last}")


def check_discovery(data, snapshot, schema, table, expected):
    validate(data, DISCOVERY)
    if (data["target_schema"], data["target_table"]) != (schema, table):
        raise ValueError("Discovery changed requested target")
    names = [c["name"] for c in data["columns"]]
    ordinals = [c["ordinal"] for c in data["columns"]]
    if not names or any(not n.strip() for n in names) or len(set(names)) != len(names):
        raise ValueError("Missing or duplicate columns")
    if ordinals != list(range(1, len(names) + 1)):
        raise ValueError("Columns must have consecutive ordinals in target order")
    if expected and names != expected:
        raise ValueError("Discovered columns differ from supplied ordered column manifest")
    if not data["enumeration_complete"] or data["unresolved"]:
        raise ValueError("Discovery incomplete: " + "; ".join(data["unresolved"]))
    check_evidence(data["entrypoints"], snapshot)
    for col in data["columns"]:
        check_evidence(col["evidence"], snapshot)


def check_trace(data, snapshot, schema, table, column):
    validate(data, TRACE)
    if data["target_column"] != column:
        raise ValueError("Worker returned a different column")
    nodes = {n["id"]: n for n in data["nodes"]}
    if len(nodes) != len(data["nodes"]) or "" in nodes:
        raise ValueError("Duplicate or empty node IDs")
    root = nodes.get(data["root_id"])
    if not root or (root["schema"], root["table"], root["column"]) != (schema, table, column):
        raise ValueError("Root does not match target")
    incoming = {n: [] for n in nodes}
    for edge in data["edges"]:
        if edge["source_id"] not in nodes or edge["target_id"] not in nodes:
            raise ValueError("Edge references missing node")
        if not edge["expression"].strip() or not edge["business_rule"].strip():
            raise ValueError("Edge lacks derivation")
        incoming[edge["target_id"]].append(edge["source_id"])
        check_evidence(edge["evidence"], snapshot)
    visited, active = set(), set()

    def visit(node):
        if node in active:
            raise ValueError("Graph cycle: terminate at a scoped cycle node")
        if node in visited:
            return
        active.add(node)
        for source in incoming[node]:
            visit(source)
        active.remove(node)
        visited.add(node)

    visit(data["root_id"])
    if visited != set(nodes):
        raise ValueError("Disconnected nodes in column lineage")
    terminals = set()
    for id_, node in nodes.items():
        check_evidence(node["evidence"], snapshot)
        if node["terminal"] == "expanded":
            if not incoming[id_]:
                raise ValueError("Expanded node has no derivation")
        else:
            terminals.add(id_)
            if incoming[id_]:
                raise ValueError("Terminal node has upstream edges")
            if not node["boundary_reason"].strip():
                raise ValueError("Terminal node needs a stopping reason")
        if node["terminal"] == "repo_boundary":
            if node["source_type"] not in ("physical", "view", "file") or not node["search_notes"]:
                raise ValueError("Repository boundary requires external source and search evidence")
        if node["terminal"] in ("literal", "runtime") and node["source_type"] != node["terminal"]:
            raise ValueError("Constant/runtime terminal must have corresponding source type")
        if node["source_type"] == "unresolved" and node["terminal"] != "unresolved":
            raise ValueError("Unknown source cannot claim complete lineage")
    mapped = set()
    for mapping in data["mappings"]:
        if mapping["source_id"] not in terminals:
            raise ValueError("Flattened mappings must reference terminal sources")
        if not mapping["expression"].strip() or not mapping["business_rule"].strip():
            raise ValueError("Mapping lacks composed derivation")
        mapped.add(mapping["source_id"])
        check_evidence(mapping["evidence"], snapshot)
    if mapped != terminals:
        raise ValueError("Flattened mappings omit terminal sources")
    return not data["unresolved"] and all(n["terminal"] not in ("unresolved", "cycle") for n in nodes.values())


def check_review(data, snapshot):
    validate(data, REVIEW)
    check_evidence(data["evidence"], snapshot)
    if data["accepted"] and data["issues"]:
        raise ValueError("Reviewer accepted with unresolved issues")
    if not data["accepted"] and not data["issues"]:
        raise ValueError("Rejected review must explain issues")


def code_urls(items, repo_url, commit):
    return " | ".join(dict.fromkeys(
        f"{repo_url}/blob/{commit}/{quote(e['path'], safe='/')}#L{e['start_line']}-L{e['end_line']}"
        for e in items))
