import csv
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest

from lineage.contracts import TRACE, validate
from lineage.evidence import check_evidence, check_trace
from lineage.harness import Claude
from lineage.runner import HEADERS, Budgeted, run


def evidence():
    return [{"path": "job.sql", "start_line": 1, "end_line": 1,
             "quote": "CREATE TABLE mart.target AS SELECT a FROM raw.source;"}]


def trace(column="a"):
    common = {"schema": "mart", "table": "target", "column": column, "scope": "job.sql:1",
              "source_type": "physical", "boundary_reason": "", "search_notes": [], "evidence": evidence()}
    root = dict(common, id="root", terminal="expanded")
    source = dict(common, id="source", schema="raw", table="source", column="a", terminal="repo_boundary",
                  boundary_reason="Read here; no producer in repository", search_notes=["Searched raw.source in all files"])
    rule = {"source_id": "source", "dependency_kind": "value", "expression": "a",
            "business_rule": "Copy source a", "evidence": evidence()}
    return {"target_column": column, "root_id": "root", "nodes": [root, source],
            "edges": [dict(rule, target_id="root")], "mappings": [rule], "unresolved": []}


class FakeClaude:
    """Transport fake, not a test of model reasoning or SQL interpretation."""
    def __init__(self, count=1, fail_column=None, reject_once=False):
        self.count, self.fail_column, self.reject_once = count, fail_column, reject_once
        self.calls, self.active, self.peak = [], 0, 0
        self.lock = threading.Lock()

    def version(self):
        return "fake-transport-v1"

    def call(self, *, skill, schema, payload, snapshot, log):
        role = skill.parent.name
        name = payload.get("column", {}).get("name")
        with self.lock:
            self.calls.append((role, name))
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            time.sleep(.001)
            if role == "discover-lineage":
                names = ["a"] if self.count == 1 else [f"col_{i}" for i in range(self.count)]
                data = {"target_schema": "mart", "target_table": "target",
                        "columns": [{"name": c, "ordinal": i+1, "evidence": evidence()} for i, c in enumerate(names)],
                        "enumeration_complete": True, "build_context": "job.sql", "entrypoints": evidence(),
                        "alternatives": [], "unresolved": []}
            elif role == "trace-column":
                if name == self.fail_column:
                    raise ValueError("simulated provider error")
                data = trace(name)
            else:
                rejected = self.reject_once and payload["review_mode"] == "column"
                if rejected:
                    self.reject_once = False
                data = {"accepted": not rejected, "issues": ["Re-read derivation"] if rejected else [],
                        "evidence": evidence()}
            envelope = {"subtype": "success", "is_error": False, "total_cost_usd": .01,
                        "structured_output": data}
            Path(str(log) + ".stdout").write_text(json.dumps(envelope))
            return data, {"cost_usd": .01}
        finally:
            with self.lock:
                self.active -= 1


class RunTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.repo / "job.sql").write_text(evidence()[0]["quote"] + "\n")
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                        "commit", "-qm", "fixture"], check=True)
        self.args = SimpleNamespace(workspace=str(self.repo), repo_url="https://github.com/example/etl",
                                    schema="mart", table="target", out=str(self.root / "out"), columns=None,
                                    context=None, model="test", timeout=5, max_turns=10, call_budget=1,
                                    run_budget=100, attempts=1, concurrency=4)

    def tearDown(self):
        self.temp.cleanup()

    def test_success_csv_and_zero_call_resume(self):
        fake = FakeClaude()
        result = run(self.args, fake)
        self.assertTrue(result["complete_within_repo"])
        with (Path(self.args.out) / "mapping.csv").open() as f:
            rows = list(csv.reader(f))
        self.assertEqual(rows[0], HEADERS)
        self.assertEqual(rows[1][0:3], ["mart", "target", "a"])
        self.assertIn("/blob/", rows[1][-1])
        second = FakeClaude()
        self.assertTrue(run(self.args, second)["complete_within_repo"])
        self.assertEqual(second.calls, [])

    def test_205_columns_bounded_concurrency(self):
        fake = FakeClaude(count=205)
        report = run(self.args, fake)
        self.assertEqual(report["column_count"], 205)
        self.assertTrue(report["complete_within_repo"])
        self.assertLessEqual(fake.peak, 4)
        self.assertEqual(len(fake.calls), 412)

    def test_failed_column_remains_visible_then_resumes(self):
        fake = FakeClaude(count=3, fail_column="col_1")
        first = run(self.args, fake)
        self.assertFalse(first["complete_within_repo"])
        self.assertEqual(first["columns"][1]["status"], "failed")
        self.assertFalse((Path(self.args.out) / "mapping.csv").exists())
        retry = FakeClaude(count=3)
        self.assertTrue(run(self.args, retry)["complete_within_repo"])
        self.assertEqual(retry.calls, [("trace-column", "col_1"), ("review-lineage", "col_1")])
        self.assertFalse((Path(self.args.out) / "mapping.partial.csv").exists())

    def test_reviewer_triggers_bounded_repair(self):
        fake = FakeClaude(reject_once=True)
        self.assertTrue(run(self.args, fake)["complete_within_repo"])
        self.assertEqual(fake.calls.count(("trace-column", "a")), 2)

    def test_changed_context_cannot_reuse_checkpoint(self):
        run(self.args, FakeClaude())
        context = self.root / "context.txt"
        context.write_text("different environment")
        self.args.context = str(context)
        with self.assertRaisesRegex(ValueError, "inputs changed"):
            run(self.args, FakeClaude())

    def test_tampered_snapshot_cannot_resume(self):
        run(self.args, FakeClaude())
        (Path(self.args.out) / "input/job.sql").write_text("changed")
        with self.assertRaisesRegex(ValueError, "snapshot changed"):
            run(self.args, FakeClaude())

    def test_dirty_workspace_rejected(self):
        (self.repo / "job.sql").write_text("uncommitted SQL")
        with self.assertRaisesRegex(ValueError, "clean"):
            run(self.args, FakeClaude())

    def test_snapshot_ignores_git_archive_substitution_attributes(self):
        (self.repo / ".gitattributes").write_text("job.sql export-ignore\nversion.txt export-subst\n")
        (self.repo / "version.txt").write_text("$Format:%H$\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                        "commit", "-qm", "attributes"], check=True)
        self.assertTrue(run(self.args, FakeClaude())["complete_within_repo"])
        self.assertEqual((Path(self.args.out) / "input/version.txt").read_text(), "$Format:%H$\n")

    def test_budget_reservations_prevent_dispatch(self):
        out = self.root / "budget"
        out.mkdir()
        class Failing:
            def call(self, **kwargs):
                raise TimeoutError("provider timed out")
        b = Budgeted(Failing(), out, limit=1, call_cap=1)
        with self.assertRaises(TimeoutError):
            b.call(log=out / "first")
        # Failed/unknown-cost calls keep the whole reservation, across restarts.
        restored = Budgeted(Failing(), out, limit=1, call_cap=1)
        with self.assertRaisesRegex(ValueError, "budget exhausted"):
            restored.call(log=out / "second")

    def test_expected_columns_gate_discovery(self):
        p = self.root / "columns.json"
        p.write_text('["a", "missing"]')
        self.args.columns = str(p)
        fake = FakeClaude()
        with self.assertRaisesRegex(ValueError, "column manifest"):
            run(self.args, fake)
        self.assertFalse(any(role == "trace-column" for role, _ in fake.calls))

    def test_quote_graph_and_mapping_guards(self):
        self.assertTrue(check_trace(trace(), self.repo, "mart", "target", "a"))
        broken = trace()
        broken["mappings"] = []
        with self.assertRaisesRegex(ValueError, "omit terminal"):
            check_trace(broken, self.repo, "mart", "target", "a")
        broken = trace()
        broken["nodes"][1]["evidence"][0]["quote"] = "invented SQL"
        with self.assertRaisesRegex(ValueError, "Quote"):
            check_trace(broken, self.repo, "mart", "target", "a")
        broken = trace()
        broken["nodes"].append(dict(broken["nodes"][1], id="unconnected"))
        with self.assertRaisesRegex(ValueError, "Disconnected"):
            check_trace(broken, self.repo, "mart", "target", "a")

    def test_cycle_and_temp_boundary_rejected(self):
        broken = trace()
        broken["edges"].append(dict(broken["edges"][0], source_id="root", target_id="source"))
        with self.assertRaisesRegex(ValueError, "cycle"):
            check_trace(broken, self.repo, "mart", "target", "a")
        broken = trace()
        broken["nodes"][1]["source_type"] = "temp"
        with self.assertRaisesRegex(ValueError, "boundary"):
            check_trace(broken, self.repo, "mart", "target", "a")

    def test_unresolved_is_valid_but_incomplete(self):
        partial = trace()
        partial["nodes"][1].update(terminal="unresolved", source_type="unresolved")
        self.assertFalse(check_trace(partial, self.repo, "mart", "target", "a"))

    def test_path_escape_and_bad_line_rejected(self):
        item = evidence()[0]
        item["path"] = "../repo/job.sql"
        with self.assertRaisesRegex(ValueError, "escapes"):
            check_evidence([item], self.repo)
        item = evidence()[0]
        item["end_line"] = 900
        with self.assertRaisesRegex(ValueError, "range"):
            check_evidence([item], self.repo)

    def test_csv_preserves_commas_quotes_and_newlines(self):
        from lineage.runner import write_csv
        p = self.root / "test.csv"
        rows = [["", "t", "a", "", "s", "b", "physical", 'CASE\nWHEN x IN (1,2) THEN "y" END', "copy", "url"]]
        write_csv(p, rows)
        with p.open(newline="") as f:
            self.assertEqual(list(csv.reader(f))[1:], rows)

    def test_schema_does_not_accept_bool_as_line_number(self):
        bad = trace()
        bad["nodes"][0]["evidence"][0]["start_line"] = True
        with self.assertRaisesRegex(ValueError, "integer"):
            validate(bad, TRACE)


class HarnessTests(unittest.TestCase):
    def test_timeout_kills_process_group(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            fake = p / "claude"
            fake.write_text('''#!/usr/bin/env python3
import subprocess, sys, time
subprocess.Popen([sys.executable, "-c", "import time; from pathlib import Path; time.sleep(.4); Path('escaped').write_text('bad')"])
time.sleep(20)
''')
            fake.chmod(0o755)
            with self.assertRaises(TimeoutError):
                Claude("test", .15, 5, 1, str(fake)).call(skill=p / "s", schema={}, payload={}, snapshot=p, log=p / "l")
            time.sleep(.5)
            self.assertFalse((p / "escaped").exists())

    def test_real_subprocess_contract_and_flags_with_fake_binary(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            fake = p / "claude"
            fake.write_text('''#!/usr/bin/env python3
import json, sys
assert "--bare" in sys.argv and "--restricted" in sys.argv
assert sys.argv[sys.argv.index("--tools") + 1] == "Read,Glob,Grep"
assert "--dangerously-skip-permissions" not in sys.argv
payload = json.load(sys.stdin)
print(json.dumps({"subtype":"success", "structured_output": payload, "total_cost_usd":0.03}))
''')
            fake.chmod(0o755)
            data, meta = Claude("test", 5, 5, 1, str(fake)).call(
                skill=p / "skill.md", schema={"type": "object"}, payload={"hello": "world"},
                snapshot=p, log=p / "call")
            self.assertEqual(data, {"hello": "world"})
            self.assertEqual(meta["cost_usd"], .03)

    def test_error_envelope_is_not_a_success(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            fake = p / "claude"
            fake.write_text('#!/usr/bin/env python3\nprint(\'{"subtype":"error_max_turns","is_error":true}\')\n')
            fake.chmod(0o755)
            with self.assertRaisesRegex(ValueError, "error_max_turns"):
                Claude("test", 5, 5, 1, str(fake)).call(skill=p / "s", schema={}, payload={}, snapshot=p, log=p / "l")


if __name__ == "__main__":
    unittest.main()
