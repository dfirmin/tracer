#!/usr/bin/env python3
"""assemble.py <run_dir> — join manifest + findings into lineage.csv, dependencies.csv, gaps.md, report.json.

The only deterministic code that shapes output, and it never reads ETL source: it reshapes what
the agents concluded and cited. lineage.csv holds value sources only (one row per terminal value
source); join/filter/group/window/order/control edges go to dependencies.csv, deduplicated per
target table, so the mapping file stays a mapping file.
"""
import csv, json, sys
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from finding_hash import finding_hash  # noqa: E402

COLUMNS = ["Target Schema", "Target Table", "Target Column",
           "Source Schema", "Source Table", "Source Column", "Source Type",
           "Mapping Rule", "Mapping Business Rule", "Code URL"]
DEP_COLUMNS = ["Target Schema", "Target Table", "Dependency Kind",
               "Source Schema", "Source Table", "Source Column", "Predicate", "Business Rule", "Code URL"]

run = Path(sys.argv[1])
meta = json.loads((run / "run.json").read_text())
manifest = json.loads((run / "manifest.json").read_text())
base = meta["code_url_base"].rstrip("/")
tgt = manifest["target"]


def url(evidence):
    links = []
    for e in evidence or []:
        path = quote(e["path"].removeprefix("repo/"), safe="/")
        links.append(f"{base}/{path}#L{e['start_line']}-L{e['end_line']}")
    return " | ".join(dict.fromkeys(links))


def safe(name):
    return "".join(c if c.isalnum() or c == "_" else "_" for c in name)


# ---- completeness ---------------------------------------------------------------------------
# A run is complete only when every reason below is zero and the conductor enumerated every
# column. Each reason is counted separately in report.json so a partial run says why.
REASONS = ("missing", "failed", "unresolved", "low_confidence",
           "review_rejected", "review_stale", "review_missing")
reasons = {r: 0 for r in REASONS}
review_policy = meta.get("review", "none")


def review_required(finding):
    if review_policy == "all":
        return True
    if review_policy == "low":
        return finding.get("confidence") != "high" or bool(finding.get("unresolved"))
    return False


def review_status(finding):
    """not_required | accepted | rejected | stale | missing — relative to this finding's version."""
    if not review_required(finding):
        return "not_required"
    review = finding.get("review")
    if not review:
        return "missing"
    if review.get("finding_hash") != finding_hash(finding):
        return "stale"
    return "accepted" if review.get("accepted") else "rejected"


rows, deps, gaps, missing, coverage = [], {}, [], [], []
for col in manifest["columns"]:
    f = run / "findings" / f"{safe(col['name'])}.json"
    failed_marker = run / "findings" / f"{safe(col['name'])}.failed.json"
    if not f.exists():
        if failed_marker.exists():
            why = json.loads(failed_marker.read_text()).get("unresolved") or ["tracer session failed"]
            reasons["failed"] += 1
            coverage.append({"column": col["name"], "status": "failed", "unresolved": why})
            gaps.append((col["name"], "failed", why))
            rows.append({"Target Schema": tgt["schema"], "Target Table": tgt["name"], "Target Column": col["name"],
                         "Source Schema": "", "Source Table": "", "Source Column": "", "Source Type": "unresolved",
                         "Mapping Rule": "", "Mapping Business Rule": "; ".join(why), "Code URL": url(col.get("evidence"))})
        else:
            missing.append(col["name"])
            reasons["missing"] += 1
            coverage.append({"column": col["name"], "status": "missing"})
        continue
    finding = json.loads(f.read_text())
    nodes = {n["id"]: n for n in finding.get("nodes", [])}
    unresolved = finding.get("unresolved") or []
    problems = []
    if finding.get("failed"):                      # a pre-1.1 failure marker left in place of a finding
        problems.append("failed")
    if unresolved:
        problems.append("unresolved")
    if finding.get("confidence") == "low":
        problems.append("low_confidence")
    rstatus = review_status(finding)
    if rstatus in ("rejected", "stale", "missing"):
        problems.append(f"review_{rstatus}")
    for p in problems:
        reasons[p] += 1
    status = "failed" if "failed" in problems else ("complete" if not problems else "partial")
    coverage.append({"column": col["name"], "status": status, "confidence": finding.get("confidence"),
                     "unresolved": unresolved, "review": rstatus, "problems": problems})
    if status != "complete":
        whys = list(unresolved)
        if finding.get("confidence") == "low" and not unresolved:
            whys.append("low confidence")
        if rstatus == "rejected":
            whys += [f"review: {i}" for i in finding["review"].get("issues", [])] or ["review rejected"]
        elif rstatus == "stale":
            whys.append("review is of an earlier version of this finding")
        elif rstatus == "missing":
            whys.append("review required but not done")
        gaps.append((col["name"], finding.get("confidence"), whys or ["failed"]))

    mappings = finding.get("mappings") or []
    if not mappings:
        rows.append({"Target Schema": tgt["schema"], "Target Table": tgt["name"], "Target Column": col["name"],
                     "Source Schema": "", "Source Table": "", "Source Column": "", "Source Type": "unresolved",
                     "Mapping Rule": "", "Mapping Business Rule": "; ".join(unresolved) or "no sources traced",
                     "Code URL": url(col.get("evidence"))})
    for m in mappings:
        s = nodes.get(m["source_id"], {})
        rows.append({
            "Target Schema": tgt["schema"], "Target Table": tgt["name"], "Target Column": col["name"],
            "Source Schema": s.get("schema", ""), "Source Table": s.get("table", ""),
            "Source Column": s.get("column", "") if s.get("source_type") not in ("literal", "runtime") else "",
            "Source Type": s.get("source_type", "unresolved"),
            "Mapping Rule": m["mapping_rule"], "Mapping Business Rule": m["mapping_business_rule"],
            "Code URL": url(m.get("evidence")),
        })
    for e in finding.get("edges", []):
        if e.get("dependency_kind", "value") == "value":
            continue
        s = nodes.get(e["source_id"], {})
        key = (e["dependency_kind"], s.get("schema"), s.get("table"), s.get("column"), e["expression"])
        deps.setdefault(key, {
            "Target Schema": tgt["schema"], "Target Table": tgt["name"], "Dependency Kind": e["dependency_kind"],
            "Source Schema": s.get("schema", ""), "Source Table": s.get("table", ""), "Source Column": s.get("column", ""),
            "Predicate": e["expression"], "Business Rule": e["business_rule"], "Code URL": url(e.get("evidence")),
        })

enumeration_complete = manifest.get("enumeration_complete") is True
complete = enumeration_complete and not any(reasons.values())
suffix = "" if complete else ".partial"
for name in ("lineage.csv", "lineage.partial.csv"):
    (run / name).unlink(missing_ok=True)
with (run / f"lineage{suffix}.csv").open("w", newline="", encoding="utf-8") as fh:
    w = csv.DictWriter(fh, fieldnames=COLUMNS, quoting=csv.QUOTE_ALL)
    w.writeheader(); w.writerows(rows)
with (run / "dependencies.csv").open("w", newline="", encoding="utf-8") as fh:
    w = csv.DictWriter(fh, fieldnames=DEP_COLUMNS, quoting=csv.QUOTE_ALL)
    w.writeheader(); w.writerows(deps.values())

lines = [f"# Gaps — {tgt['schema']}.{tgt['name']}", "",
         f"Columns: {len(manifest['columns'])}  Rows: {len(rows)}  Gaps: {len(gaps)}  Missing findings: {len(missing)}", ""]
if not enumeration_complete:
    lines += ["- **(enumeration)**: the conductor did not confirm that every target column is listed", ""]
for name, conf, whys in gaps:
    lines.append(f"- **{name}** ({conf}):")
    lines.extend(f"  - {w}" for w in whys)
for name in missing:
    lines.append(f"- **{name}**: no finding file (tracer never ran)")
(run / "gaps.md").write_text("\n".join(lines) + "\n")

spend = 0.0
if (run / "budget.json").exists():
    spend = sum(e["usd"] for e in json.loads((run / "budget.json").read_text()))
report = {"target": f"{tgt['schema']}.{tgt['name']}", "commit": meta.get("sha"), "run_identity": meta.get("run_identity"),
          "complete": complete, "review_policy": review_policy,
          "incomplete_reasons": {**reasons, "enumeration_incomplete": not enumeration_complete},
          "column_count": len(manifest["columns"]), "row_count": len(rows), "dependency_count": len(deps),
          "enumeration_complete": manifest.get("enumeration_complete"), "columns": coverage,
          "committed_spend_usd": round(spend, 4), "mapping_file": f"lineage{suffix}.csv"}
(run / "report.json").write_text(json.dumps(report, indent=2) + "\n")
why = " ".join(f"{k}={v}" for k, v in report["incomplete_reasons"].items() if v)
print(f"rows={len(rows)} deps={len(deps)} gaps={len(gaps)} complete={complete}" + (f" ({why})" if why else ""))
