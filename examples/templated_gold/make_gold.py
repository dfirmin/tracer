#!/usr/bin/env python3
"""Regenerate examples/templated_gold/ after editing examples/templated/:  python3 examples/templated_gold/make_gold.py .

The lineage below is the hand-written truth for the prod context (context.txt). This script only
resolves each citation to its line number and fails if a quote is missing or ambiguous, so gold stays
consistent with the fixture files. Writes responses/ (fake-model answers for the tests) and the path-free
lineage_keys.csv / dependency_keys.csv that bin/compare scores live runs against."""
import csv, json, sys
from pathlib import Path

ROOT = Path(sys.argv[1])            # tracer checkout
FX = ROOT / "examples" / "templated"
P = "repo/examples/templated/"      # how the test harness mounts it
GOLD = FX.parent / "templated_gold"     # outside the fixture, so a live run cannot read the answers
(GOLD / "responses").mkdir(parents=True, exist_ok=True)


def line_of(f, quote, nth=1):
    lines = (FX / f).read_text().splitlines()
    hits = [i + 1 for i, l in enumerate(lines) if quote in l]
    assert len(hits) >= nth, (f, quote, hits)
    if nth == 1:
        assert len(hits) == 1, f"ambiguous quote {quote!r} in {f}: lines {hits}"
    return hits[nth - 1]


def ev(f, quote, nth=1):
    n = line_of(f, quote, nth)
    return {"path": P + f, "start_line": n, "end_line": n, "quote": quote}


def span(f, first, last):
    return {"path": P + f, "start_line": line_of(f, first), "end_line": line_of(f, last), "quote": first}


LOAD, MERGE, BACK, BTEQ = "notebooks/load_claim_fact.py", "notebooks/apply_adjustments.sql", "notebooks/generic_backfill.py", "bteq/stage_claim_lines.bteq"
BJOB, NJOB, PROD, STAGE = "jobs/backfill_claim_fact.json", "jobs/claim_fact_nightly.json", "conf/env/prod.yaml", "scripts/run_stage.sh"

# ---- manifest -------------------------------------------------------------------------------
def stmt(id_, f, first, last, writes, kind, role, order=None, context=None):
    s = {"id": id_, **span(f, first, last), "writes": writes, "write_kind": kind, "role": role}
    if order is not None:
        s["execution_order"] = order
    if context:
        s["context"] = context
    return s

statements = [
    stmt("s1_stage_claim_lines", BTEQ, "INSERT INTO ${STG_DB}.claim_line_stg", "GROUP BY c.claim_id, c.policy_id;",
         "${STG_DB}.claim_line_stg", "insert", "Teradata staging (table truncated first)", 1, "scripts/run_stage.sh prod: STG_DB=STG, RAW_DB=RAW_CLAIMS"),
    stmt("s2_load_claim_fact", LOAD, 'spark.sql(f"""', '""")', "{target}", "insert",
         "nightly full load; positional INSERT OVERWRITE (DDL order)", 2, "job claim_fact_nightly, env=prod"),
    stmt("s3_apply_adjustments", MERGE, "MERGE INTO ${edw_schema}.claim_fact AS t", "WHEN MATCHED THEN UPDATE SET",
         "${edw_schema}.claim_fact", "merge", "post-load adjustment; updates paid_amount only", 3, "job claim_fact_nightly"),
    stmt("s4_generic_backfill", BACK, 'spark.sql(f"INSERT INTO {dst}', 'spark.sql(f"INSERT INTO {dst}', "{dst}", "insert",
         "ad hoc backfill of rows before 2024-01-01; positional", None, "job backfill_claim_fact"),
]
s2_expr = {"claim_id": ("l.claim_id", "SELECT l.claim_id,"), "policy_number": ("p.policy_number", "p.policy_number,"),
           "line_of_business": ("UPPER(TRIM(p.lob_cd))", "UPPER(TRIM(p.lob_cd)) AS line_of_business"),
           "paid_amount": ("l.line_amount", "l.line_amount AS paid_amount"), "load_dt": ("current_date()", "current_date() AS load_dt")}
columns = []
for i, (name, (expr, q)) in enumerate(s2_expr.items(), 1):
    a = [{"statement_id": "s2_load_claim_fact", "expression": expr, "evidence": [ev(LOAD, q)]}]
    if name == "paid_amount":
        a.append({"statement_id": "s3_apply_adjustments", "expression": "t.paid_amount + a.adj_amount",
                  "evidence": [ev(MERGE, "UPDATE SET t.paid_amount = t.paid_amount + a.adj_amount")]})
    a.append({"statement_id": "s4_generic_backfill", "expression": f"{name} (position {i} of the columns widget)",
              "evidence": [ev(BACK, "SELECT {cols} FROM {src}")]})
    columns.append({"name": name, "ordinal": i, "expression": expr, "evidence": [ev(LOAD, q)], "assignments": a})

def obj(name, kind, e, scope=None, note=None):
    o = {"name": name, "kind": kind, "evidence": [e]}
    if scope: o["scope"] = scope
    if note: o["note"] = note
    return o

manifest = {
    "target": {"schema": "EDW", "name": "claim_fact", "kind": "table"},
    "build_sites": [
        {"role": "nightly full load (INSERT OVERWRITE)", "shape": "PySpark spark.sql f-string", "evidence": [ev(LOAD, "INSERT OVERWRITE {target}")]},
        {"role": "post-load MERGE (paid_amount only)", "shape": "Databricks SQL notebook", "evidence": [ev(MERGE, "MERGE INTO ${edw_schema}.claim_fact AS t")]},
        {"role": "ad hoc backfill via generic loader", "shape": "PySpark spark.sql f-string; target from job JSON", "evidence": [ev(BJOB, '"target_table": "EDW.claim_fact"')]},
    ],
    "statements": statements,
    "columns": columns,
    "object_index": [
        obj("STG.claim_line_stg", "physical", ev(BTEQ, "INSERT INTO ${STG_DB}.claim_line_stg"), "written by s1 each night after DELETE ALL"),
        obj("RAW_CLAIMS.policy", "physical", ev(LOAD, "JOIN {cfg['raw_schema']}.policy p")),
        obj("RAW_CLAIMS.claim", "physical", ev(BTEQ, "FROM ${RAW_DB}.claim c")),
        obj("RAW_CLAIMS.claim_line", "physical", ev(BTEQ, "JOIN ${RAW_DB}.claim_line cl")),
        obj("RAW_CLAIMS.claim_adjustment", "physical", ev(MERGE, "FROM ${raw_schema}.claim_adjustment")),
        obj("EDW.claim_fact_hist", "physical", ev(BJOB, '"source_table": "EDW.claim_fact_hist"'), note="read by the backfill job only"),
    ],
    "templating": [
        {"placeholder": "{target}", "resolved_to": "EDW.claim_fact", "evidence": [ev(LOAD, "target = f\"{cfg['edw_schema']}.{table}\""), ev(NJOB, '"target_table": "claim_fact"')]},
        {"placeholder": "{cfg['stg_schema']}", "resolved_to": "STG (prod)", "evidence": [ev(PROD, "stg_schema: STG")]},
        {"placeholder": "{cfg['raw_schema']}", "resolved_to": "RAW_CLAIMS (prod)", "evidence": [ev(PROD, "raw_schema: RAW_CLAIMS")]},
        {"placeholder": "${edw_schema}", "resolved_to": "EDW", "evidence": [ev(NJOB, '"edw_schema": "EDW"')]},
        {"placeholder": "${raw_schema}", "resolved_to": "RAW_CLAIMS", "evidence": [ev(NJOB, '"raw_schema": "RAW_CLAIMS"')]},
        {"placeholder": "${STG_DB}", "resolved_to": "STG (prod)", "evidence": [ev(STAGE, "export STG_DB=STG RAW_DB=RAW_CLAIMS")]},
        {"placeholder": "${RAW_DB}", "resolved_to": "RAW_CLAIMS (prod)", "evidence": [ev(STAGE, "export STG_DB=STG RAW_DB=RAW_CLAIMS")]},
        {"placeholder": "{dst}", "resolved_to": "EDW.claim_fact", "evidence": [ev(BJOB, '"target_table": "EDW.claim_fact"')]},
        {"placeholder": "{src}", "resolved_to": "EDW.claim_fact_hist", "evidence": [ev(BJOB, '"source_table": "EDW.claim_fact_hist"')]},
        {"placeholder": "{cols}", "resolved_to": "claim_id, policy_number, line_of_business, paid_amount, load_dt", "evidence": [ev(BJOB, '"columns": "claim_id')]},
    ],
    "notes": ("Column order is ddl/claim_fact.sql: both inserts are positional. apply_adjustments MERGE updates only paid_amount. "
              "The backfill target exists only in jobs/backfill_claim_fact.json (generic_backfill.py is shared). "
              "Prod bindings per the context; dev values are in conf/env/dev.yaml and scripts/run_stage.sh."),
    "enumeration_complete": True,
}

# ---- findings -------------------------------------------------------------------------------
SEARCH = ["rg -n -i '<table>' repo/ — read sites only", "no INSERT/MERGE/CREATE/saveAsTable writes it in the repo"]

def node(id_, schema, table, column, stype, terminal, e, scope="", reason=""):
    n = {"id": id_, "schema": schema, "table": table, "column": column, "scope": scope, "source_type": stype,
         "terminal": terminal, "boundary_reason": reason, "evidence": [e]}
    if terminal == "repo_boundary":
        n["search_notes"] = [s.replace("<table>", f"{schema}.{table}" if schema else table) for s in SEARCH]
    return n

def raw(id_, table, column, e):
    return node(id_, "RAW_CLAIMS", table, column, "physical", "repo_boundary", e, reason="read only; populated outside this repo")

def hist(id_, column):
    return node(id_, "EDW", "claim_fact_hist", column, "physical", "repo_boundary", ev(BJOB, '"source_table": "EDW.claim_fact_hist"'),
                reason="backfill source; no writer in this repo")

def edge(s, t, kind, expr, rule, e):
    return {"source_id": s, "target_id": t, "dependency_kind": kind, "expression": expr, "business_rule": rule, "evidence": [e]}

def mapping(s, rule, biz, *es):
    return {"source_id": s, "mapping_rule": rule, "mapping_business_rule": biz, "evidence": list(es)}

# shared row-selection dependencies
def load_deps(target):
    return ([raw("p_policy_id", "policy", "policy_id", ev(LOAD, "ON p.policy_id = l.policy_id")),
             raw("p_active", "policy", "active_flag", ev(LOAD, "WHERE p.active_flag = 'Y'"))],
            [edge("p_policy_id", target, "join", "p.policy_id = l.policy_id", "Only claims whose policy exists in RAW_CLAIMS.policy are loaded.", ev(LOAD, "ON p.policy_id = l.policy_id")),
             edge("p_active", target, "filter", "p.active_flag = 'Y'", "Only active policies are loaded.", ev(LOAD, "WHERE p.active_flag = 'Y'"))])

def stage_deps(target):
    return ([raw("cl_status", "claim_line", "status_cd", ev(BTEQ, "WHERE cl.status_cd <> 'V'")),
             raw("cl_claim_id", "claim_line", "claim_id", ev(BTEQ, "ON cl.claim_id = c.claim_id"))],
            [edge("cl_status", target, "filter", "cl.status_cd <> 'V'", "Voided claim lines are excluded from staging.", ev(BTEQ, "WHERE cl.status_cd <> 'V'")),
             edge("cl_claim_id", target, "join", "cl.claim_id = c.claim_id", "Claims are staged with their lines.", ev(BTEQ, "ON cl.claim_id = c.claim_id"))])

def backfill_edge(src, t, col):
    return edge(src, t, "value", f"EDW.claim_fact_hist.{col} (positional, columns widget)",
                f"Backfilled rows carry {col} from EDW.claim_fact_hist as is.", ev(BACK, "SELECT {cols} FROM {src}"))

def finding(col, nodes, edges, mappings, confidence="high"):
    return {"target_column": col, "root_id": "root", "confidence": confidence, "unresolved": [],
            "nodes": nodes, "edges": edges, "mappings": mappings}

def root(col, e, scope="EDW.claim_fact after nightly load (s2) or backfill (s4)"):
    return node("root", "EDW", "claim_fact", col, "physical", "expanded", e, scope=scope)

findings = {}

# claim_id: staging -> load, plus backfill
n, e = [root("claim_id", ev(LOAD, "SELECT l.claim_id,"))], []
n.append(node("stg_claim_id", "STG", "claim_line_stg", "claim_id", "physical", "expanded", ev(BTEQ, "SELECT c.claim_id,"), scope="s1 nightly staging"))
n.append(raw("c_claim_id", "claim", "claim_id", ev(BTEQ, "SELECT c.claim_id,")))
n.append(hist("h_claim_id", "claim_id"))
e += [edge("stg_claim_id", "root", "value", "l.claim_id", "Claim id is carried from staging.", ev(LOAD, "SELECT l.claim_id,")),
      edge("c_claim_id", "stg_claim_id", "value", "c.claim_id", "Staging takes the claim id from RAW_CLAIMS.claim.", ev(BTEQ, "SELECT c.claim_id,")),
      backfill_edge("h_claim_id", "root", "claim_id")]
dn, de = load_deps("root"); n += dn; e += de
dn, de = stage_deps("stg_claim_id"); n += dn; e += de
findings["claim_id"] = finding("claim_id", n, e, [
    mapping("c_claim_id", "RAW_CLAIMS.claim.claim_id → STG.claim_line_stg.claim_id (grouped per claim, voided lines excluded) → claim_id; rows kept only for active policies",
            "The claim's id from the claims system.", ev(BTEQ, "SELECT c.claim_id,"), ev(LOAD, "SELECT l.claim_id,")),
    mapping("h_claim_id", "EDW.claim_fact_hist.claim_id → claim_id for rows with load_dt < 2024-01-01 (backfill)", "Historical claims keep their archived id.", ev(BACK, "SELECT {cols} FROM {src}"))])

# policy_number and line_of_business: straight from policy at load
for col, expr, q, rule, biz in [
    ("policy_number", "p.policy_number", "p.policy_number,", "RAW_CLAIMS.policy.policy_number", "The policy number of the claim's policy."),
    ("line_of_business", "UPPER(TRIM(p.lob_cd))", "UPPER(TRIM(p.lob_cd)) AS line_of_business", "UPPER(TRIM(RAW_CLAIMS.policy.lob_cd))", "The policy's line-of-business code, trimmed and upper-cased.")]:
    srccol = "policy_number" if col == "policy_number" else "lob_cd"
    n = [root(col, ev(LOAD, q)), raw("p_src", "policy", srccol, ev(LOAD, q)), hist("h_src", col)]
    e = [edge("p_src", "root", "value", expr, biz, ev(LOAD, q)), backfill_edge("h_src", "root", col)]
    dn, de = load_deps("root"); n += dn; e += de
    findings[col] = finding(col, n, e, [
        mapping("p_src", f"{rule} → {col}; joined on policy_id, active policies only", biz, ev(LOAD, q)),
        mapping("h_src", f"EDW.claim_fact_hist.{col} → {col} for rows with load_dt < 2024-01-01 (backfill)", f"Historical rows keep their archived {col}.", ev(BACK, "SELECT {cols} FROM {src}"))])

# paid_amount: load (staged sum) or backfill, then MERGE adds approved adjustments
UPD = "UPDATE SET t.paid_amount = t.paid_amount + a.adj_amount"
n = [root("paid_amount", ev(MERGE, UPD), scope="EDW.claim_fact after apply_adjustments MERGE (s3)"),
     node("prior", "EDW", "claim_fact", "paid_amount", "physical", "expanded", ev(LOAD, "l.line_amount AS paid_amount"),
          scope="EDW.claim_fact before the MERGE: nightly load (s2) or backfill (s4)"),
     node("stg_amt", "STG", "claim_line_stg", "line_amount", "physical", "expanded", ev(BTEQ, "SUM(cl.line_amt),"), scope="s1 nightly staging"),
     raw("cl_amt", "claim_line", "line_amt", ev(BTEQ, "SUM(cl.line_amt),")),
     hist("h_paid", "paid_amount"),
     node("adj", "", "a", "adj_amount", "cte", "expanded", ev(MERGE, "SELECT claim_id, SUM(adj_amount) AS adj_amount"), scope="MERGE USING subquery a"),
     raw("adj_amt", "claim_adjustment", "adj_amount", ev(MERGE, "SELECT claim_id, SUM(adj_amount) AS adj_amount")),
     raw("adj_status", "claim_adjustment", "adj_status", ev(MERGE, "WHERE adj_status = 'APPROVED'")),
     raw("adj_claim_id", "claim_adjustment", "claim_id", ev(MERGE, "ON t.claim_id = a.claim_id"))]
e = [edge("prior", "root", "value", "t.paid_amount", "The loaded amount is the starting value.", ev(MERGE, UPD)),
     edge("adj", "root", "value", "+ a.adj_amount", "Approved adjustments are added on matching claims.", ev(MERGE, UPD)),
     edge("adj_claim_id", "root", "join", "t.claim_id = a.claim_id", "Adjustments apply only to claims already in claim_fact.", ev(MERGE, "ON t.claim_id = a.claim_id")),
     edge("stg_amt", "prior", "value", "l.line_amount", "Nightly load takes the staged claim total.", ev(LOAD, "l.line_amount AS paid_amount")),
     backfill_edge("h_paid", "prior", "paid_amount"),
     edge("cl_amt", "stg_amt", "value", "SUM(cl.line_amt)", "Staging sums non-void line amounts per claim.", ev(BTEQ, "SUM(cl.line_amt),")),
     edge("adj_amt", "adj", "value", "SUM(adj_amount)", "Adjustments are summed per claim.", ev(MERGE, "SELECT claim_id, SUM(adj_amount) AS adj_amount")),
     edge("adj_status", "adj", "filter", "adj_status = 'APPROVED'", "Only approved adjustments count.", ev(MERGE, "WHERE adj_status = 'APPROVED'"))]
dn, de = load_deps("prior"); n += dn; e += de
dn, de = stage_deps("stg_amt"); n += dn; e += de
findings["paid_amount"] = finding("paid_amount", n, e, [
    mapping("cl_amt", "SUM(RAW_CLAIMS.claim_line.line_amt) per claim, status_cd <> 'V' → STG.claim_line_stg.line_amount → paid_amount (active policies), then + approved adjustments",
            "Total of the claim's non-void line amounts, plus approved adjustments.", ev(BTEQ, "SUM(cl.line_amt),"), ev(LOAD, "l.line_amount AS paid_amount"), ev(MERGE, UPD)),
    mapping("h_paid", "EDW.claim_fact_hist.paid_amount → paid_amount for backfilled rows, then + approved adjustments",
            "Archived paid amount for historical claims, plus approved adjustments.", ev(BACK, "SELECT {cols} FROM {src}"), ev(MERGE, UPD)),
    mapping("adj_amt", "paid_amount + SUM(RAW_CLAIMS.claim_adjustment.adj_amount) WHERE adj_status = 'APPROVED', matched on claim_id",
            "Approved adjustments are added to the paid amount.", ev(MERGE, "SELECT claim_id, SUM(adj_amount) AS adj_amount"), ev(MERGE, UPD))])

# load_dt: runtime at load, or archived
n = [root("load_dt", ev(LOAD, "current_date() AS load_dt")),
     node("now", "", "", "current_date()", "runtime", "runtime", ev(LOAD, "current_date() AS load_dt"), reason="current_date() when the nightly load runs"),
     hist("h_dt", "load_dt")]
e = [edge("now", "root", "value", "current_date()", "Stamped with the load date.", ev(LOAD, "current_date() AS load_dt")), backfill_edge("h_dt", "root", "load_dt")]
dn, de = load_deps("root"); n += dn; e += de
findings["load_dt"] = finding("load_dt", n, e, [
    mapping("now", "current_date() at load time → load_dt", "The date the nightly load ran.", ev(LOAD, "current_date() AS load_dt")),
    mapping("h_dt", "EDW.claim_fact_hist.load_dt → load_dt for rows with load_dt < 2024-01-01 (backfill)", "Historical rows keep their original load date.", ev(BACK, "SELECT {cols} FROM {src}"))])

# ---- write ----------------------------------------------------------------------------------
def dump(p, d):
    p.write_text(json.dumps(d, indent=1, ensure_ascii=False) + "\n")

dump(GOLD / "responses" / "conductor.json", manifest)
for col, f in findings.items():
    dump(GOLD / "responses" / f"column-tracer-{col}.json", f)

with (GOLD / "lineage_keys.csv").open("w", newline="") as fh:
    w = csv.writer(fh); w.writerow(["Target Column", "Source Schema", "Source Table", "Source Column", "Source Type"])
    for col in s2_expr:
        f = findings[col]; nodes = {x["id"]: x for x in f["nodes"]}
        for m in f["mappings"]:
            s = nodes[m["source_id"]]
            w.writerow([col, s["schema"], s["table"], "" if s["source_type"] in ("literal", "runtime") else s["column"], s["source_type"]])
deps = set()
for f in findings.values():
    nodes = {x["id"]: x for x in f["nodes"]}
    for e in f["edges"]:
        if e["dependency_kind"] != "value":
            s = nodes[e["source_id"]]; deps.add((e["dependency_kind"], s["schema"], s["table"], s["column"]))
with (GOLD / "dependency_keys.csv").open("w", newline="") as fh:
    w = csv.writer(fh); w.writerow(["Dependency Kind", "Source Schema", "Source Table", "Source Column"])
    w.writerows(sorted(deps))
print(f"gold: {len(columns)} columns, {sum(len(f['mappings']) for f in findings.values())} lineage keys, {len(deps)} dependency keys")
