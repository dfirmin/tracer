#!/usr/bin/env python3
"""Tests for the tracer pipeline using a fake `claude` binary.

    python3 -m unittest discover -s tests -v

They exercise orchestration and the checks — validation feedback, retry, budget, resume,
review/repair, CSV shape — with canned model output. They say nothing about how well a real
model traces lineage; that is what docs/evaluation.md is for.
"""
import csv, json, os, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))


class Run:
    """A throwaway /work layout: repo/ is a committed copy of examples/, harness is this checkout."""

    def __init__(self, review="none", env=None):
        self.tmp = Path(tempfile.mkdtemp(prefix="tracer-test-"))
        self.work = self.tmp / "work"
        (self.work / ".claude").mkdir(parents=True)
        shutil.copytree(ROOT / "harness" / "agents", self.work / ".claude" / "agents")
        shutil.copytree(ROOT / "harness" / "skills", self.work / ".claude" / "skills")
        shutil.copy(ROOT / "harness" / "CLAUDE.md", self.work / "CLAUDE.md")
        repo = self.work / "repo"
        shutil.copytree(ROOT / "examples", repo / "examples")
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "fixture"], cwd=repo, check=True)
        subprocess.run(["git", "remote", "add", "origin", "https://github.com/org/etl.git"], cwd=repo, check=True)
        self.sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        shutil.copy(ROOT / "tests" / "fake-claude", self.bin / "claude")
        self.log = self.tmp / "calls.jsonl"
        self.env = {**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}",
                    "WORK": str(self.work), "LINEAGE_HOME": str(ROOT),
                    "FAKE_RESPONSES": str(ROOT / "tests" / "fixtures" / "responses"), "FAKE_LOG": str(self.log),
                    "LINEAGE_REVIEW": review, "LINEAGE_CONCURRENCY": "3", **(env or {})}
        self.run_name = "t"

    def tracer(self, *args):
        return subprocess.run([str(ROOT / "bin" / "tracer"), "mart.daily_sales", "--run", self.run_name, *args],
                              env=self.env, capture_output=True, text=True)

    @property
    def run_dir(self):
        return self.work / ".lineage" / self.run_name

    def calls(self):
        return [json.loads(l)["key"] for l in self.log.read_text().splitlines()] if self.log.exists() else []

    def csv(self, name="lineage.csv"):
        with (self.run_dir / name).open(newline="") as f:
            return list(csv.DictReader(f))

    def close(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.r = Run()
        self.addCleanup(self.r.close)

    def test_clean_run_produces_value_only_csv_and_dependencies(self):
        p = self.r.tracer()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        rows = self.r.csv()
        self.assertEqual([r["Target Column"] for r in rows], ["order_id", "net_usd", "net_usd", "net_usd", "segment", "segment"])
        net = [r for r in rows if r["Target Column"] == "net_usd"]
        self.assertEqual({r["Source Column"] for r in net}, {"amount", "discount", "rate"})
        self.assertNotIn("customer_id", [r["Source Column"] for r in rows])       # join key is not a source
        self.assertNotIn("is_current", [r["Source Column"] for r in rows])        # filter is not a source
        self.assertIn(f"blob/{self.r.sha}/examples/etl/01_stage.sql#L4-L4", net[0]["Code URL"])
        lit = [r for r in rows if r["Source Type"] == "literal"][0]
        self.assertEqual(lit["Source Column"], "")
        deps = self.r.csv("dependencies.csv")
        self.assertEqual({(d["Dependency Kind"], d["Source Column"]) for d in deps},
                         {("filter", "is_current"), ("join", "currency"), ("filter", "status")})
        self.assertNotIn("[", rows[0]["Mapping Business Rule"])
        report = json.loads((self.r.run_dir / "report.json").read_text())
        self.assertTrue(report["complete"])
        self.assertEqual(sorted(self.r.calls()), sorted(["conductor", "column-tracer-order_id", "column-tracer-net_usd", "column-tracer-segment"]))  # parallel: order varies

    def test_headless_flags_are_locked_down(self):
        self.r.tracer()
        argv = json.loads(self.r.log.read_text().splitlines()[1])["argv"]
        for flag in ("--agent", "column-tracer", "--permission-mode", "dontAsk", "--json-schema", "--max-budget-usd", "--output-format", "json"):
            self.assertIn(flag, argv)

    def test_bad_citation_is_rejected_then_retried_with_feedback(self):
        self.r.env["FAKE_FAIL_ONCE"] = "column-tracer-net_usd"
        p = self.r.tracer()
        self.assertEqual(p.returncode, 0, p.stderr)
        calls = self.r.calls()
        self.assertEqual(calls.count("column-tracer-net_usd"), 2)
        second = [json.loads(l) for l in self.r.log.read_text().splitlines() if '"column-tracer-net_usd"' in l][1]
        self.assertIn("quote does not occur", second["prompt"])

    def test_failed_column_becomes_partial_csv_and_gap(self):
        self.r.env["FAKE_FAIL_ONCE"] = "column-tracer-segment"
        self.r.env["LINEAGE_ATTEMPTS"] = "1"
        p = self.r.tracer()
        self.assertEqual(p.returncode, 3)
        self.assertTrue((self.r.run_dir / "lineage.partial.csv").exists())
        self.assertFalse((self.r.run_dir / "lineage.csv").exists())
        seg = [r for r in self.r.csv("lineage.partial.csv") if r["Target Column"] == "segment"]
        self.assertEqual(seg[0]["Source Type"], "unresolved")
        self.assertIn("segment", (self.r.run_dir / "gaps.md").read_text())

    def test_resume_skips_finished_columns(self):
        self.r.tracer()
        (self.r.run_dir / "findings" / "net_usd.json").unlink()
        self.r.log.unlink()
        p = self.r.tracer()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.r.calls(), ["column-tracer-net_usd"])

    def test_changed_commit_refuses_to_resume(self):
        self.r.tracer()
        repo = self.r.work / "repo"
        (repo / "examples" / "note.txt").write_text("x")
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "moved"], cwd=repo, check=True)
        p = self.r.tracer()
        self.assertEqual(p.returncode, 2)
        self.assertIn("new --run", p.stderr)

    def test_run_budget_stops_dispatch(self):
        # conductor reserves 4×2=8 then reports 3; each tracer reserves 2 and reports 3.
        # 3 + 3 + 3 = 9 > 8.5, so the third tracer is never dispatched.
        self.r.env.update(LINEAGE_RUN_BUDGET="8.5", LINEAGE_CONCURRENCY="1", FAKE_COST="3")
        p = self.r.tracer()
        self.assertEqual(p.returncode, 3)
        self.assertIn("budget exhausted", p.stderr)
        ledger = json.loads((self.r.run_dir / "budget.json").read_text())
        self.assertEqual([e["state"] for e in ledger], ["reported", "reported", "reported"])
        self.assertEqual(len(self.r.calls()), 3)
        self.assertIn("no finding file", (self.r.run_dir / "gaps.md").read_text())

    def test_review_all_reviews_every_column(self):
        r = Run(review="all")
        self.addCleanup(r.close)
        p = r.tracer()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(r.calls().count("lineage-reviewer-net_usd"), 1)
        finding = json.loads((r.run_dir / "findings" / "net_usd.json").read_text())
        self.assertTrue(finding["review"]["accepted"])

    def test_review_low_skips_high_confidence(self):
        r = Run(review="low")
        self.addCleanup(r.close)
        r.tracer()
        self.assertFalse(any(k.startswith("lineage-reviewer") for k in r.calls()))

    def test_rejected_review_triggers_one_repair(self):
        r = Run(review="all")
        self.addCleanup(r.close)
        resp = Path(tempfile.mkdtemp()) / "responses"
        shutil.copytree(ROOT / "tests" / "fixtures" / "responses", resp)
        rej = json.loads((resp / "lineage-reviewer-segment.json").read_text())
        rej.update(accepted=False, issues=["repo/examples/etl/02_target.sql: literal default missing"])
        (resp / "lineage-reviewer-segment.json").write_text(json.dumps(rej))
        r.env["FAKE_RESPONSES"] = str(resp)
        r.tracer()
        calls = r.calls()
        self.assertEqual(calls.count("column-tracer-segment"), 2)      # trace + repair
        self.assertEqual(calls.count("lineage-reviewer-segment"), 2)   # review + re-review
        self.assertTrue((r.run_dir / "findings" / "segment.rejected.json").exists())


class CheckTests(unittest.TestCase):
    """The validator on its own: entailment and graph invariants."""

    def setUp(self):
        self.r = Run()
        self.addCleanup(self.r.close)
        self.good = json.loads((ROOT / "tests" / "fixtures" / "responses" / "column-tracer-net_usd.json").read_text())

    def problems(self, finding, column="net_usd"):
        f = self.r.tmp / "f.json"
        f.write_text(json.dumps(finding))
        p = subprocess.run([str(ROOT / "bin" / "check"), "finding", str(f), "--column", column, "--work", str(self.r.work)],
                           capture_output=True, text=True)
        return p.returncode, p.stdout

    def test_good_finding_passes(self):
        self.assertEqual(self.problems(self.good), (0, ""))

    def test_quote_must_occur_at_lines(self):
        bad = json.loads(json.dumps(self.good))
        bad["nodes"][0]["evidence"][0]["quote"] = "nope"
        rc, out = self.problems(bad)
        self.assertEqual(rc, 2)
        self.assertIn("quote does not occur", out)

    def test_dependency_only_source_cannot_be_mapped(self):
        bad = json.loads(json.dumps(self.good))
        bad["mappings"].append({"source_id": "fx_cur", "mapping_rule": "x", "mapping_business_rule": "x",
                                "evidence": bad["nodes"][3]["evidence"]})
        rc, out = self.problems(bad)
        self.assertIn("a dependency, not a source", out)

    def test_every_value_terminal_needs_a_mapping(self):
        bad = json.loads(json.dumps(self.good))
        bad["mappings"] = bad["mappings"][:1]
        rc, out = self.problems(bad)
        self.assertIn("has no mapping", out)

    def test_cycle_and_disconnected_are_rejected(self):
        bad = json.loads(json.dumps(self.good))
        bad["edges"].append({"source_id": "root", "target_id": "vt_net", "dependency_kind": "value", "expression": "x",
                             "business_rule": "x", "evidence": bad["nodes"][0]["evidence"]})
        bad["nodes"][1]["terminal"] = "expanded"
        rc, out = self.problems(bad)
        self.assertIn("cycle", out)
        bad = json.loads(json.dumps(self.good))
        bad["nodes"].append(dict(bad["nodes"][2], id="orphan"))
        rc, out = self.problems(bad)
        self.assertIn("not connected", out)

    def test_boundary_needs_search_notes(self):
        bad = json.loads(json.dumps(self.good))
        del bad["nodes"][2]["search_notes"]
        rc, out = self.problems(bad)
        self.assertIn("search_notes", out)


if __name__ == "__main__":
    unittest.main()
