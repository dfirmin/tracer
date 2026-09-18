#!/usr/bin/env python3
"""assemble.py <run_dir> — join manifest + findings into lineage.csv and gaps.md.

This is the only deterministic code in the pipeline, and it never looks at ETL source:
it reshapes what the agents concluded and cited. Every row keeps a Code URL so a human
can check the claim against the commit it was read from.
"""
import csv, json, sys
from pathlib import Path

COLUMNS = ["Target Schema", "Target Table", "Target Column",
           "Source Schema", "Source Table", "Source Column", "Source Type",
           "Mapping Rule", "Mapping Business Rule", "Code URL"]

run = Path(sys.argv[1])
meta = json.loads((run / "run.json").read_text())
manifest = json.loads((run / "manifest.json").read_text())
base = meta["code_url_base"].rstrip("/")
tgt = manifest["target"]


def url(path, lines):
    if not path:
        return ""
    path = path.removeprefix("repo/")
    if lines and lines.get("start"):
        end = lines.get("end") or lines["start"]
        return f"{base}/{path}#L{lines['start']}-L{end}"
    return f"{base}/{path}"


rows, gaps, missing = [], [], []
for col in manifest["columns"]:
    safe = "".join(c if c.isalnum() or c == "_" else "_" for c in col["name"])
    f = run / "findings" / f"{safe}.json"
    if not f.exists():
        missing.append(col["name"])
        continue
    finding = json.loads(f.read_text())
    if finding.get("unresolved_reason") or finding.get("confidence") == "low":
        gaps.append((col["name"], finding.get("confidence"), finding.get("unresolved_reason") or "low confidence"))
    sources = finding.get("sources") or [{
        "source_schema": "", "source_table": "", "source_column": "", "source_type": "unknown",
        "mapping_rule": "", "mapping_business_rule": finding.get("unresolved_reason", ""),
        "code_path": col.get("defined_at", {}).get("path"), "code_lines": col.get("defined_at", {}).get("lines"),
    }]
    for s in sources:
        rows.append({
            "Target Schema": tgt["schema"], "Target Table": tgt["name"], "Target Column": col["name"],
            "Source Schema": s.get("source_schema", ""), "Source Table": s.get("source_table", ""),
            "Source Column": s.get("source_column", ""), "Source Type": s.get("source_type", "unknown"),
            "Mapping Rule": s.get("mapping_rule", ""), "Mapping Business Rule": s.get("mapping_business_rule", ""),
            "Code URL": url(s.get("code_path"), s.get("code_lines")),
        })

with (run / "lineage.csv").open("w", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=COLUMNS)
    w.writeheader(); w.writerows(rows)

lines = [f"# Gaps — {tgt['schema']}.{tgt['name']}", "",
         f"Columns: {len(manifest['columns'])}  Rows: {len(rows)}  Gaps: {len(gaps)}  Missing findings: {len(missing)}", ""]
for name, conf, why in gaps:
    lines.append(f"- **{name}** ({conf}): {why}")
for name in missing:
    lines.append(f"- **{name}**: no finding file (tracer never ran)")
(run / "gaps.md").write_text("\n".join(lines) + "\n")
print(f"rows={len(rows)} gaps={len(gaps)} missing={len(missing)}")
