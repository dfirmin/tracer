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

    def tracer(self, *args, target="mart.daily_sales"):
        return subprocess.run([str(ROOT / "bin" / "tracer"), target, "--run", self.run_name, *args],
                              env=self.env, capture_output=True, text=True)

    def report(self):
        return json.loads((self.run_dir / "report.json").read_text())

    def responses(self, **overrides):
        """A private copy of the canned responses with some files' JSON patched; sets FAKE_RESPONSES."""
        resp = Path(tempfile.mkdtemp(prefix="tracer-resp-")) / "responses"
        shutil.copytree(ROOT / "tests" / "fixtures" / "responses", resp)
        for name, patch in overrides.items():
            f = resp / f"{name}.json"
            d = json.loads(f.read_text()); d.update(patch); f.write_text(json.dumps(d))
        self.env["FAKE_RESPONSES"] = str(resp)
        return resp

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
        r.responses(**{"lineage-reviewer-segment": dict(accepted=False, issues=["repo/examples/etl/02_target.sql: literal default missing"])})
        r.tracer()
        calls = r.calls()
        self.assertEqual(calls.count("column-tracer-segment"), 2)      # trace + repair
        self.assertEqual(calls.count("lineage-reviewer-segment"), 2)   # review + re-review
        self.assertTrue((r.run_dir / "findings" / "segment.rejected.json").exists())


class CompletenessAndResumeTests(unittest.TestCase):
    """Delivery 1.1 (issues #1/#2): a run is complete only when it really is, and resume never
    reuses a failed finding, a stale review, or another run's inputs."""

    def setUp(self):
        self.r = Run()
        self.addCleanup(self.r.close)

    def test_failed_finding_is_retried_on_resume(self):
        self.r.env.update(FAKE_FAIL_ONCE="column-tracer-segment", LINEAGE_ATTEMPTS="1")
        p = self.r.tracer()
        self.assertEqual(p.returncode, 3)
        findings = self.r.run_dir / "findings"
        self.assertTrue((findings / "segment.failed.json").exists())
        self.assertFalse((findings / "segment.json").exists())
        self.assertEqual(self.r.report()["incomplete_reasons"]["failed"], 1)
        seg = [x for x in self.r.csv("lineage.partial.csv") if x["Target Column"] == "segment"]
        self.assertEqual(seg[0]["Source Type"], "unresolved")

        self.r.log.unlink()                                             # FAKE_FAIL_ONCE already fired: next call succeeds
        p = self.r.tracer()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.r.calls(), ["column-tracer-segment"])     # only the failed column is re-traced
        self.assertTrue((findings / "segment.json").exists())
        self.assertFalse((findings / "segment.failed.json").exists())
        self.assertTrue(self.r.report()["complete"])

    def test_legacy_failed_marker_in_finding_file_is_retried(self):
        self.r.tracer()
        f = self.r.run_dir / "findings" / "segment.json"
        f.write_text(json.dumps({"target_column": "segment", "failed": True, "unresolved": ["old harness"]}))
        self.r.log.unlink()
        p = self.r.tracer()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.r.calls(), ["column-tracer-segment"])

    def test_rejected_review_that_survives_repair_is_not_complete(self):
        r = Run(review="all")
        self.addCleanup(r.close)
        r.responses(**{"lineage-reviewer-segment": dict(accepted=False, issues=["repo/examples/etl/02_target.sql: literal default missing"])})
        p = r.tracer()
        self.assertEqual(p.returncode, 3)
        rep = r.report()
        self.assertFalse(rep["complete"])
        self.assertEqual(rep["incomplete_reasons"]["review_rejected"], 1)
        seg = [c for c in rep["columns"] if c["column"] == "segment"][0]
        self.assertEqual(seg["review"], "rejected")
        self.assertIn("review:", (r.run_dir / "gaps.md").read_text())
        self.assertTrue((r.run_dir / "lineage.partial.csv").exists())

    def test_enumeration_incomplete_is_not_complete(self):
        self.r.responses(conductor=dict(enumeration_complete=False))
        p = self.r.tracer()
        self.assertEqual(p.returncode, 3)
        rep = self.r.report()
        self.assertFalse(rep["complete"])
        self.assertTrue(rep["incomplete_reasons"]["enumeration_incomplete"])
        self.assertEqual(sum(v for k, v in rep["incomplete_reasons"].items() if k != "enumeration_incomplete"), 0)
        self.assertIn("enumeration", (self.r.run_dir / "gaps.md").read_text())

    def test_review_is_bound_to_the_finding_version(self):
        r = Run(review="all")
        self.addCleanup(r.close)
        r.tracer()
        self.assertTrue(r.report()["complete"])
        review = json.loads((r.run_dir / "reviews" / "segment.json").read_text())
        self.assertEqual(len(review["finding_hash"]), 64)
        # the finding changes under the review: assemble alone must call it stale …
        f = r.run_dir / "findings" / "segment.json"
        d = json.loads(f.read_text()); d["mappings"][0]["mapping_business_rule"] = "edited after review"; f.write_text(json.dumps(d))
        subprocess.run([sys.executable, str(ROOT / "bin" / "assemble.py"), str(r.run_dir)], env=r.env, check=True, capture_output=True)
        rep = r.report()
        self.assertFalse(rep["complete"])
        self.assertEqual(rep["incomplete_reasons"]["review_stale"], 1)
        # … and a resumed run re-reviews that column without re-tracing it
        r.log.unlink()
        p = r.tracer()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(r.calls(), ["lineage-reviewer-segment"])
        self.assertTrue((r.run_dir / "reviews" / "segment.stale.json").exists())
        self.assertTrue(r.report()["complete"])

    def test_missing_required_review_is_added_without_retracing(self):
        self.r.tracer()                                                 # review=none: traced, unreviewed
        self.assertTrue(self.r.report()["complete"])
        self.r.log.unlink()
        self.r.env["LINEAGE_REVIEW"] = "all"
        p = self.r.tracer()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(sorted(self.r.calls()), ["lineage-reviewer-net_usd", "lineage-reviewer-order_id", "lineage-reviewer-segment"])
        rep = self.r.report()
        self.assertEqual(rep["review_policy"], "all")
        self.assertTrue(rep["complete"])

    def test_different_target_refuses_to_resume(self):
        self.r.tracer()
        self.r.log.unlink()
        p = self.r.tracer(target="mart.other_table")
        self.assertEqual(p.returncode, 2)
        self.assertIn("target: mart.daily_sales → mart.other_table", p.stderr)
        self.assertEqual(self.r.calls(), [])

    def test_changed_harness_refuses_to_resume(self):
        self.r.tracer()
        rep = self.r.report()
        self.assertEqual(len(rep["run_identity"]), 16)
        run_json = self.r.run_dir / "run.json"
        d = json.loads(run_json.read_text()); d["identity"]["harness_version"] = "0000000000000000"; run_json.write_text(json.dumps(d))
        p = self.r.tracer()
        self.assertEqual(p.returncode, 2)
        self.assertIn("harness_version", p.stderr)

    def test_run_without_identity_refuses_to_resume(self):
        self.r.tracer()
        run_json = self.r.run_dir / "run.json"
        d = json.loads(run_json.read_text()); del d["identity"]; run_json.write_text(json.dumps(d))
        p = self.r.tracer()
        self.assertEqual(p.returncode, 2)
        self.assertIn("new --run", p.stderr)


class RunInputsTests(unittest.TestCase):
    """Delivery 1.3: --notes / --context / --columns reach the agents, are part of the run's
    identity, and --columns only ever cross-checks enumeration."""

    def setUp(self):
        self.r = Run()
        self.addCleanup(self.r.close)
        self.files = self.r.tmp / "inputs"
        self.files.mkdir()

    def file(self, name, text):
        f = self.files / name
        f.write_text(text)
        return str(f)

    def prompts(self, key):
        return [json.loads(l)["prompt"] for l in self.r.log.read_text().splitlines() if json.loads(l)["key"] == key]

    def test_notes_and_context_reach_every_agent_and_are_recorded(self):
        notes = self.file("notes.md", "Loaders live under examples/etl/.")
        ctx = self.file("ctx.txt", "Job: nightly. Env: prod.")
        p = self.r.tracer("--notes", notes, "--context", ctx)
        self.assertEqual(p.returncode, 0, p.stderr)
        inputs = self.r.run_dir / "inputs"
        self.assertEqual((inputs / "notes.md").read_text(), "Loaders live under examples/etl/.")
        for key in ("conductor", "column-tracer-net_usd"):
            prompt = self.prompts(key)[0]
            self.assertIn(f"Repo notes: {inputs / 'notes.md'}".replace(str(self.r.work) + "/", ""), prompt)
            self.assertIn("Never cite them as evidence", prompt)
            self.assertIn("Execution context:", prompt)
        self.assertNotIn("Expected columns:", self.prompts("conductor")[0])
        ident = json.loads((self.r.run_dir / "run.json").read_text())["identity"]
        self.assertNotEqual(ident["notes"], "none")
        self.assertNotEqual(ident["context"], "none")
        self.assertEqual(ident["expected_columns"], "none")
        self.assertEqual(self.r.report()["inputs"], ["context.txt", "notes.md"])

    def test_repo_lineage_md_is_used_as_notes(self):
        repo = self.r.work / "repo"
        (repo / "LINEAGE.md").write_text("Targets are built by examples/etl/02_target.sql.")
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "notes"], cwd=repo, check=True)
        self.r.tracer()
        self.assertIn("02_target.sql", (self.r.run_dir / "inputs" / "notes.md").read_text())
        self.assertIn("Repo notes:", self.prompts("conductor")[0])

    def test_resume_reuses_recorded_inputs_and_refuses_changed_ones(self):
        ctx = self.file("ctx.txt", "Env: prod.")
        self.r.tracer("--context", ctx)
        (self.r.run_dir / "findings" / "net_usd.json").unlink()
        self.r.log.unlink()
        p = self.r.tracer()                                              # flag omitted: recorded context reused
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("Execution context:", self.prompts("column-tracer-net_usd")[0])
        p = self.r.tracer("--context", self.file("ctx2.txt", "Env: dev."))
        self.assertEqual(p.returncode, 2)
        self.assertIn("context:", p.stderr)
        p = self.r.tracer("--notes", self.file("n.md", "new"))           # a new input on resume also refuses
        self.assertEqual(p.returncode, 2)
        self.assertIn("notes: none →", p.stderr)

    def test_expected_columns_match(self):
        p = self.r.tracer("--columns", self.file("c.json", '["ORDER_ID", "net_usd", "segment"]'))
        self.assertEqual(p.returncode, 0, p.stderr)
        rep = self.r.report()
        self.assertEqual(rep["expected_columns"], {"provided": 3, "missing_from_manifest": [], "not_in_expected": [], "order_matches": True})
        self.assertIn("Expected columns:", self.prompts("conductor")[0])
        self.assertNotIn("Expected columns:", self.prompts("column-tracer-net_usd")[0])

    def test_expected_columns_mismatch_blocks_completeness_without_inventing(self):
        p = self.r.tracer("--columns", self.file("c.json", '["order_id", "segment", "net_usd", "region"]'))
        self.assertEqual(p.returncode, 3)
        rep = self.r.report()
        self.assertFalse(rep["complete"])
        self.assertEqual(rep["incomplete_reasons"]["expected_columns_mismatch"], 1)
        self.assertEqual(rep["expected_columns"]["missing_from_manifest"], ["region"])
        self.assertFalse(rep["expected_columns"]["order_matches"])
        self.assertNotIn("region", {x["Target Column"] for x in self.r.csv("lineage.partial.csv")})
        self.assertIn("(expected columns)", (self.r.run_dir / "gaps.md").read_text())

    def test_bad_inputs_are_rejected(self):
        self.assertEqual(self.r.tracer("--columns", self.file("c.json", '{"a": 1}')).returncode, 2)
        self.assertEqual(self.r.tracer("--notes", "/nonexistent").returncode, 2)


class TelemetryTests(unittest.TestCase):
    """Delivery 1.2: every call and stage is recorded; bin/metrics separates retry classes."""

    def setUp(self):
        self.r = Run(env={"LINEAGE_BACKOFF_BASE": "0.01"})
        self.addCleanup(self.r.close)

    def attempts(self):
        return [json.loads(l) for l in (self.r.run_dir / "attempts.jsonl").read_text().splitlines()]

    def metrics(self):
        return json.loads((self.r.run_dir / "metrics.json").read_text())

    def test_clean_run_records_every_call_and_stage(self):
        p = self.r.tracer()
        self.assertEqual(p.returncode, 0, p.stderr)
        a = self.attempts()
        self.assertEqual(len(a), 4)                                      # conductor + 3 tracers
        self.assertTrue(all(x["outcome"] == "ok" and x["class"] == "first" for x in a))
        self.assertTrue(all(x["num_turns"] == 3 and x["output_tokens"] == 400 for x in a))
        m = self.metrics()
        self.assertEqual(m["sessions"], 4)
        self.assertEqual(m["turns"]["total"], 12)
        self.assertEqual(m["retries"], {"transient": 0, "structural": 0, "semantic": 0})
        self.assertEqual(m["columns"], {"total": 3, "resolved": 3})
        self.assertAlmostEqual(m["cost_per_resolved_column_usd"], round(0.2 / 3, 4))
        for s in ("run", "discover", "trace", "review", "assemble"):
            self.assertIsNotNone(m["wall_clock_s"][s], s)
        self.assertEqual(set(m["stages"]), {"discover", "trace"})
        self.assertIn("metrics:", p.stdout)

    def test_rate_limit_is_transient_and_does_not_use_an_attempt(self):
        self.r.env.update(FAKE_TRANSIENT_ONCE="column-tracer-net_usd", LINEAGE_ATTEMPTS="1")
        p = self.r.tracer()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)            # one attempt allowed, still succeeded
        net = [x for x in self.attempts() if x["label"] == "trace-net_usd"]
        self.assertEqual([(x["outcome"], x["class"]) for x in net], [("transient_failure", "first"), ("ok", "transient")])
        self.assertEqual(net[0]["rate_limit_hits"], 1)
        self.assertIn("429", (self.r.run_dir / "logs" / "trace-net_usd.1.stderr").read_text())
        m = self.metrics()
        self.assertEqual(m["retries"]["transient"], 1)
        self.assertEqual(m["retries"]["structural"], 0)
        self.assertEqual(m["rate_limit_hits"], 1)

    def test_bad_citation_counts_as_structural_retry(self):
        self.r.env["FAKE_FAIL_ONCE"] = "column-tracer-net_usd"
        self.r.tracer()
        net = [x for x in self.attempts() if x["label"] == "trace-net_usd"]
        self.assertEqual([(x["outcome"], x["class"]) for x in net], [("rejected", "first"), ("ok", "structural")])
        self.assertEqual(self.metrics()["retries"]["structural"], 1)

    def test_review_repair_counts_as_semantic(self):
        r = Run(review="all")
        self.addCleanup(r.close)
        r.responses(**{"lineage-reviewer-segment": dict(accepted=False, issues=["repo/examples/etl/02_target.sql: x"])})
        r.tracer()
        m = json.loads((r.run_dir / "metrics.json").read_text())
        self.assertEqual(m["retries"]["semantic"], 1)
        self.assertEqual(set(m["stages"]), {"discover", "trace", "review", "repair"})
        self.assertEqual(m["stages"]["review"]["sessions"], 4)          # 3 reviews + 1 re-review of segment
        self.assertEqual(m["columns"]["resolved"], 2)

    def test_resume_is_a_second_invocation(self):
        self.r.tracer()
        (self.r.run_dir / "findings" / "net_usd.json").unlink()
        self.r.tracer()
        m = self.metrics()
        self.assertEqual(m["invocations"], 2)
        self.assertEqual(m["stages"]["trace"]["sessions"], 3)            # net_usd's label was reused
        self.assertEqual(m["stages"]["trace"]["calls"], 4)


GOLD = ROOT / "examples" / "templated_gold"


class TemplatedFixtureTests(unittest.TestCase):
    """Delivery 1.5: the templated fixture (EDW.claim_fact) — names only in config/job JSON, four writers
    across BTEQ / PySpark / Databricks SQL, a partial MERGE, a generic backfill. The gold responses play
    the model; the point is that the harness carries this shape end to end and bin/compare scores it."""

    def setUp(self):
        self.r = Run(env={"FAKE_RESPONSES": str(GOLD / "responses")})
        self.addCleanup(self.r.close)
        self.r.run_name = "claims"

    def run_fixture(self, *args):
        return self.r.tracer("--context", str(GOLD / "context.txt"), *args, target="EDW.claim_fact")

    def prompt(self, key):
        return [json.loads(l)["prompt"] for l in self.r.log.read_text().splitlines() if json.loads(l)["key"] == key][0]

    def compare(self):
        p = subprocess.run([str(ROOT / "bin" / "compare"), str(self.r.run_dir), str(GOLD)], capture_output=True, text=True)
        return p.returncode, p.stdout, json.loads((self.r.run_dir / "compare.json").read_text())

    def test_gold_run_is_complete_and_matches_gold(self):
        p = self.run_fixture()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        rep = self.r.report()
        self.assertTrue(rep["complete"])
        self.assertEqual([c["column"] for c in rep["columns"]], ["claim_id", "policy_number", "line_of_business", "paid_amount", "load_dt"])
        rc, out, res = self.compare()
        self.assertEqual(rc, 0, out)
        self.assertEqual((res["lineage"]["matched"], res["lineage"]["expected"]), (11, 11))
        self.assertEqual(res["dependencies"]["recall"], 1.0)
        self.assertIn("lineage 11/11", out)
        self.assertEqual(len(list((self.r.run_dir / "excerpts").glob("*.txt"))), 4)

    def test_paid_amount_prompt_has_all_three_writers_in_order(self):
        self.run_fixture()
        pr = self.prompt("column-tracer-paid_amount")
        self.assertIn("Assigned by 3 statement(s), in execution order:", pr)
        order = [pr.index(f"[{s}]") for s in ("s2_load_claim_fact", "s3_apply_adjustments", "s4_generic_backfill")]
        self.assertEqual(order, sorted(order))                                # unordered backfill goes last
        self.assertIn("UPDATE SET t.paid_amount = t.paid_amount + a.adj_amount", pr)
        self.assertIn("WHERE p.active_flag = 'Y'", pr)
        self.assertIn("STG.claim_line_stg (physical)", pr)                    # templated read matched by table name
        self.assertIn("written by [s1_stage_claim_lines]", pr)
        self.assertIn("{cfg['stg_schema']} → STG (prod)", pr)
        self.assertIn("{dst} → EDW.claim_fact", pr)
        self.assertIn("Execution context:", pr)

    def test_policy_number_prompt_omits_unrelated_statement(self):
        self.run_fixture()
        pr = self.prompt("column-tracer-policy_number")
        self.assertIn("Assigned by 2 statement(s)", pr)
        self.assertNotIn("MERGE INTO", pr)
        self.assertNotIn("RAW_CLAIMS.claim_adjustment", pr)

    def test_compare_scores_a_degraded_run(self):
        self.run_fixture()
        path = self.r.run_dir / "lineage.csv"
        rows = self.r.csv()
        kept = [x for x in rows if not (x["Target Column"] == "paid_amount" and x["Source Table"] == "claim_adjustment")]
        kept.append(dict(rows[0], **{"Source Table": "claim_header"}))        # a wrong source for claim_id
        with path.open("w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0])); w.writeheader(); w.writerows(kept)
        rc, out, res = self.compare()
        self.assertEqual(rc, 4)
        self.assertEqual(res["lineage"]["matched"], 10)
        self.assertEqual(res["lineage"]["recall"], round(10 / 11, 4))
        self.assertEqual(res["lineage"]["precision"], round(10 / 11, 4))
        self.assertEqual(res["columns"]["paid_amount"]["missing"], ["raw_claims.claim_adjustment.adj_amount"])
        self.assertEqual(res["columns"]["claim_id"]["extra"], ["raw_claims.claim_header.claim_id"])

    def test_wrong_column_list_is_caught_by_expected_columns(self):
        cols = self.r.tmp / "cols.json"
        cols.write_text('["claim_id", "policy_number", "line_of_business", "paid_amount", "load_dt", "reserve_amount"]')
        p = self.run_fixture("--columns", str(cols))
        self.assertEqual(p.returncode, 3)
        self.assertEqual(self.r.report()["expected_columns"]["missing_from_manifest"], ["reserve_amount"])


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


class StatementTests(unittest.TestCase):
    """Delivery 1.4: statements and assignments in the manifest; harness-built excerpts and focused prompts."""

    def setUp(self):
        self.r = Run()
        self.addCleanup(self.r.close)
        self.manifest = json.loads((ROOT / "tests" / "fixtures" / "responses" / "conductor.json").read_text())

    def check(self, m):
        f = self.r.tmp / "m.json"
        f.write_text(json.dumps(m))
        p = subprocess.run([str(ROOT / "bin" / "check"), "manifest", str(f), "--work", str(self.r.work)], capture_output=True, text=True)
        return p.returncode, p.stdout

    def bad(self):
        return json.loads(json.dumps(self.manifest))

    def test_fixture_manifest_passes(self):
        self.assertEqual(self.check(self.manifest), (0, ""))

    def test_statements_are_required_and_spans_are_checked(self):
        m = self.bad(); del m["statements"]
        self.assertIn("missing 'statements'", self.check(m)[1])
        m = self.bad(); m["statements"][1]["quote"] = "INSERT INTO nowhere"
        self.assertIn("statement s2_daily_sales", self.check(m)[1])
        m = self.bad(); m["statements"][1]["end_line"] = 99
        self.assertIn("outside", self.check(m)[1])
        m = self.bad(); m["statements"].append(dict(m["statements"][0]))
        self.assertIn("duplicate id", self.check(m)[1])

    def test_every_column_needs_an_assignment_inside_its_statement(self):
        m = self.bad(); m["columns"][0]["assignments"] = []
        self.assertIn("column order_id: no assignments", self.check(m)[1])
        m = self.bad(); m["columns"][0]["assignments"][0]["statement_id"] = "nope"
        self.assertIn("unknown statement_id", self.check(m)[1])
        m = self.bad(); m["columns"][1]["assignments"][0]["statement_id"] = "s1_vt_sales"   # evidence is in 02_target.sql
        self.assertIn("is outside statement s1_vt_sales", self.check(m)[1])

    def test_run_writes_excerpts_with_original_line_numbers(self):
        p = self.r.tracer()
        self.assertEqual(p.returncode, 0, p.stderr)
        ex = (self.r.run_dir / "excerpts" / "s2_daily_sales.txt").read_text().splitlines()
        self.assertEqual(ex[0], "-- repo/examples/etl/02_target.sql lines 1-9 · statement s2_daily_sales (create_table_as → mart.daily_sales)")
        self.assertEqual(ex[3], "3 |          s.net_amount * fx.rate AS net_usd,")
        self.assertEqual(len(ex), 10)
        self.assertTrue((self.r.run_dir / "excerpts" / "s1_vt_sales.txt").exists())

    def test_tracer_prompt_carries_statement_text_and_only_relevant_context(self):
        self.r.tracer()
        prompt = [json.loads(l)["prompt"] for l in self.r.log.read_text().splitlines() if json.loads(l)["key"] == "column-tracer-net_usd"][0]
        self.assertIn("Column: net_usd (ordinal 2 of 3) — s.net_amount * fx.rate", prompt)
        self.assertIn("3 |          s.net_amount * fx.rate AS net_usd,", prompt)
        self.assertIn("8 |   WHERE fx.is_current = 1", prompt)                         # row selection travels with it
        self.assertIn("- vt_sales (volatile)", prompt)
        self.assertIn("written by [s1_vt_sales]", prompt)
        self.assertIn("raw.exchange_rates (physical)", prompt)
        self.assertNotIn("raw.orders", prompt)                                         # not named by this statement
        self.assertIn("Conductor notes:", prompt)
        self.assertLess(prompt.index("Column context"), prompt.index("Trace this column"))

    def test_long_statement_is_referenced_not_inlined(self):
        self.r.env["LINEAGE_EXCERPT_MAX_LINES"] = "5"
        self.r.tracer()
        prompt = [json.loads(l)["prompt"] for l in self.r.log.read_text().splitlines() if json.loads(l)["key"] == "column-tracer-net_usd"][0]
        self.assertIn("(9 lines — too long to inline; read", prompt)
        self.assertNotIn("8 |   WHERE fx.is_current = 1", prompt)

    def test_multiple_assignments_are_all_shown_in_execution_order(self):
        repo = self.r.work / "repo"
        (repo / "examples" / "etl" / "03_fix.sql").write_text("UPDATE mart.daily_sales\nSET segment = 'RETAIL'\nWHERE segment = 'UNKNOWN';\n")
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "upd"], cwd=repo, check=True)
        m = self.bad()
        m["statements"].append({"id": "s3_fix_segment", "path": "repo/examples/etl/03_fix.sql", "start_line": 1, "end_line": 3,
                                "quote": "UPDATE mart.daily_sales", "writes": "mart.daily_sales", "write_kind": "update",
                                "role": "post-load correction", "execution_order": 3})
        seg = m["columns"][2]
        seg["assignments"].append({"statement_id": "s3_fix_segment", "expression": "'RETAIL'",
                                   "evidence": [{"path": "repo/examples/etl/03_fix.sql", "start_line": 2, "end_line": 2, "quote": "SET segment = 'RETAIL'"}]})
        seg["assignments"].reverse()                                                     # out of order on purpose
        self.assertEqual(self.check(m), (0, ""))
        self.r.responses(conductor=m)
        self.r.tracer()
        prompt = [json.loads(l)["prompt"] for l in self.r.log.read_text().splitlines() if json.loads(l)["key"] == "column-tracer-segment"][0]
        self.assertIn("Assigned by 2 statement(s), in execution order:", prompt)
        self.assertLess(prompt.index("[s2_daily_sales]"), prompt.index("[s3_fix_segment]"))
        self.assertIn("update → mart.daily_sales (post-load correction, order 3)", prompt)
        self.assertIn("2 | SET segment = 'RETAIL'", prompt)


if __name__ == "__main__":
    unittest.main()
