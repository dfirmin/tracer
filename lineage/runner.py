"""Scheduling, immutable inputs, recovery and exports. No source-language logic."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import csv
from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
from urllib.parse import urlparse

from .contracts import DISCOVERY, TRACE, REVIEW
from .evidence import check_discovery, check_trace, check_review, code_urls
from .harness import Claude

ROOT = Path(__file__).resolve().parent.parent
HEADERS = ["Target Schema", "Target Table", "Target Column", "Source Schema", "Source Table",
           "Source Column", "Source Type", "Mapping Rule", "Mapping Business Rule", "Code URL"]


class Budgeted:
    """Reserve each call's entire cap before dispatch; preserve reservations after crashes."""
    def __init__(self, harness, out, limit, call_cap):
        self.harness, self.limit, self.call_cap = harness, limit, call_cap
        self.path = out / "budget.json"
        self.entries = read_json(self.path) if self.path.exists() else []
        self.lock = threading.Lock()

    def call(self, **kwargs):
        with self.lock:
            committed = sum(e["charged_or_reserved_usd"] for e in self.entries)
            if committed + self.call_cap > self.limit + 1e-9:
                raise ValueError("Run budget exhausted; raise --run-budget to resume")
            entry = {"log": str(kwargs["log"].name), "charged_or_reserved_usd": self.call_cap,
                     "state": "reserved"}
            self.entries.append(entry)
            write_json(self.path, self.entries)
        data, meta = self.harness.call(**kwargs)
        with self.lock:
            cost = meta.get("cost_usd")
            if isinstance(cost, (int, float)) and math.isfinite(cost) and cost >= 0:
                entry["charged_or_reserved_usd"] = cost
                entry["state"] = "reported"
            write_json(self.path, self.entries)
        return data, meta


def read_json(path):
    return json.loads(Path(path).read_text())


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".write-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(value, f, indent=2, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def git(repo, *args):
    return subprocess.check_output(["git", "-c", f"safe.directory={repo}", "-C", str(repo), *args])


def snapshot(repo, destination):
    """Copy exact Git blobs, including export-ignored files; no source is parsed."""
    repo = Path(repo).resolve()
    commit = git(repo, "rev-parse", "HEAD").decode().strip()
    if git(repo, "status", "--porcelain", "--untracked-files=all").strip():
        raise ValueError("Workspace must be clean and committed for truthful commit citations")
    destination.mkdir()
    entries = []
    for entry in git(repo, "ls-tree", "-rz", "--full-tree", commit).split(b"\0"):
        if not entry:
            continue
        metadata, raw_path = entry.split(b"\t", 1)
        mode, kind, oid = metadata.split()
        path = Path(raw_path.decode("utf-8"))
        if kind != b"blob" or mode not in (b"100644", b"100755"):
            raise ValueError(f"Symlinks/submodules require explicit materialization in the input commit: {path}")
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"Unsupported snapshot path: {path}")
        entries.append((oid, path))
    blobs = subprocess.check_output(["git", "-c", f"safe.directory={repo}", "-C", str(repo),
                                     "cat-file", "--batch"],
                                    input=b"\n".join(oid for oid, _ in entries) + b"\n") if entries else b""
    offset = 0
    for oid, path in entries:
        end = blobs.index(b"\n", offset)
        header = blobs[offset:end].split()
        if header[:2] != [oid, b"blob"]:
            raise ValueError(f"Cannot read committed blob: {path}")
        size = int(header[2])
        dest = destination / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(blobs[end+1:end+1+size])
        offset = end + size + 2
    return commit


def snapshot_hash(path):
    h = hashlib.sha256()
    for file in sorted(Path(path).rglob("*")):
        if file.is_symlink():
            raise ValueError("Snapshot contains a symlink")
        if file.is_file():
            h.update(str(file.relative_to(path)).encode() + b"\0")
            h.update(hashlib.sha256(file.read_bytes()).digest())
    return h.hexdigest()


@contextmanager
def run_lock(out):
    out.mkdir(parents=True, exist_ok=True)
    with (out / ".lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("Another process owns this output directory") from exc
        yield


def call_agent(harness, role, schema, payload, snapshot, out, label, checker, attempts):
    errors = []
    for attempt in range(attempts):
        log = out / "logs" / f"{label}-{os.urandom(5).hex()}"
        log.parent.mkdir(parents=True, exist_ok=True)
        task = dict(payload, validation_feedback=errors[-1:])
        try:
            data, meta = harness.call(skill=ROOT / "skills" / role / "SKILL.md",
                                      schema=schema, payload=task, snapshot=snapshot, log=log)
            write_json(str(log) + ".json", {"result": data, "metadata": meta})
            checker(data)
            return data
        except (ValueError, OSError, TimeoutError) as exc:
            errors.append(str(exc))
            write_json(str(log) + ".error.json", {"error": str(exc)})
    raise ValueError("; ".join(errors))


def csv_row(target, source, item, repo_url, commit):
    business = f"[{item['dependency_kind']}] {item['business_rule']}"
    if source["terminal"] in ("unresolved", "cycle"):
        business = "[UNRESOLVED] " + business
    return [target["schema"], target["table"], target["column"], source["schema"],
            source["table"], source["column"], source["source_type"],
            item["expression"], business, code_urls(item["evidence"], repo_url, commit)]


def write_csv(path, rows):
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, quoting=csv.QUOTE_ALL)
        writer.writerow(HEADERS)
        writer.writerows(rows)
    os.replace(tmp, path)


def export(out, manifest, discovery, records):
    mappings, hops, coverage = [], [], []
    for col in discovery["columns"]:
        record = records[col["name"]]
        coverage.append({"column": col["name"], "status": record["status"],
                         "issues": record.get("issues", [])})
        if "trace" not in record:
            continue
        trace = record["trace"]
        nodes = {n["id"]: n for n in trace["nodes"]}
        root = nodes[trace["root_id"]]
        # Preserve per-column scope in the graph; don't collapse temporary names across jobs.
        for item in trace["mappings"]:
            mappings.append(csv_row(root, nodes[item["source_id"]], item,
                                    manifest["repo_url"], manifest["commit"]))
        for item in trace["edges"]:
            hops.append(csv_row(nodes[item["target_id"]], nodes[item["source_id"]], item,
                                manifest["repo_url"], manifest["commit"]))
    complete = all(r["status"] == "complete_within_repo" for r in records.values())
    # Use separate filenames for partial exports, and remove obsolete prior projections.
    for name in ("mapping.csv", "lineage.csv", "mapping.partial.csv", "lineage.partial.csv"):
        (out / name).unlink(missing_ok=True)
    suffix = "" if complete else ".partial"
    write_csv(out / f"mapping{suffix}.csv", mappings)
    write_csv(out / f"lineage{suffix}.csv", hops)
    write_json(out / "lineage.json", {"manifest": manifest, "discovery": discovery, "columns": records})
    costs = []
    for path in (out / "logs").glob("*.stdout"):
        try:
            costs.append(json.loads(path.read_text()).get("total_cost_usd", 0) or 0)
        except (ValueError, OSError):
            pass
    report = {"complete_within_repo": complete, "column_count": len(coverage),
              "columns": coverage, "reported_cost_usd": sum(costs),
              "cost_note": "CLI estimates; killed calls may have unreported spend.",
              "scope": "Supplied commit and execution context only; not proof of ultimate system-of-record origin.",
              "mapping_file": f"mapping{suffix}.csv", "hop_file": f"lineage{suffix}.csv"}
    write_json(out / "report.json", report)
    return report


def run(args, harness=None):
    out = Path(args.out).resolve()
    repo = Path(args.workspace).resolve()
    if out.is_relative_to(repo):
        raise ValueError("Output must be outside the input Git workspace")
    repo_url = args.repo_url.rstrip("/").removesuffix(".git")
    url = urlparse(repo_url)
    if url.scheme != "https" or not url.netloc or url.username or not re.fullmatch(r"/[^/]+/[^/]+", url.path):
        raise ValueError("--repo-url must be an HTTPS GitHub or GitHub Enterprise owner/repo URL")
    expected = read_json(args.columns) if args.columns else []
    if not isinstance(expected, list) or any(not isinstance(c, str) or not c for c in expected):
        raise ValueError("--columns must contain a JSON array of column names")
    context = Path(args.context).read_text() if args.context else ""
    if not args.schema.strip() or not args.table.strip():
        raise ValueError("Target schema and table must be nonempty")
    harness = harness or Claude(args.model, args.timeout, args.max_turns, args.call_budget)
    prompts = {str(p.relative_to(ROOT)): p.read_text() for p in sorted((ROOT / "skills").rglob("*.md"))}
    # Include the implementation so altered validation/export logic invalidates checkpoints.
    implementation = {p.name: p.read_text() for p in sorted((ROOT / "lineage").glob("*.py"))}
    config = {"schema": args.schema, "table": args.table, "repo_url": repo_url,
              "model": args.model, "context": context, "columns": expected,
              "harness_version": harness.version(), "code_hash": digest(implementation),
              "prompt_hash": digest(prompts), "review": True}
    with run_lock(out):
        input_path = out / "input"
        manifest_path = out / "manifest.json"
        commit = git(repo, "rev-parse", "HEAD").decode().strip()
        if git(repo, "status", "--porcelain", "--untracked-files=all").strip():
            raise ValueError("Workspace must be clean and committed")
        if manifest_path.exists():
            manifest = read_json(manifest_path)
            if manifest["config"] != config or manifest["commit"] != commit:
                raise ValueError("Run inputs changed; choose a new output directory")
            if manifest["snapshot_hash"] != snapshot_hash(input_path):
                raise ValueError("Saved snapshot changed; choose a new output directory")
        else:
            if input_path.exists():
                raise ValueError("Incomplete snapshot exists; choose a new output directory")
            commit = snapshot(repo, input_path)
            # References belong outside source and are explicitly injected below.
            manifest = {"commit": commit, "repo_url": repo_url, "config": config,
                        "snapshot_hash": snapshot_hash(input_path)}
            write_json(manifest_path, manifest)
        payload = {"target_schema": args.schema, "target_table": args.table,
                   "execution_context": context, "expected_columns": expected,
                   "source_root": str(input_path), "commit": commit}
        harness = Budgeted(harness, out, args.run_budget, args.call_budget)
        discover_check = lambda d: check_discovery(d, input_path, args.schema, args.table, expected)
        discovery_path = out / "discovery.json"
        if discovery_path.exists():
            discovery = read_json(discovery_path)
            discover_check(discovery)
        else:
            discovery = call_agent(harness, "discover-lineage", DISCOVERY, payload, input_path, out,
                                   "discovery", discover_check, args.attempts)
            write_json(discovery_path, discovery)
        review_payload = dict(payload, review_mode="discovery", discovery=discovery)
        audit_path = out / "discovery-review.json"
        if audit_path.exists():
            saved_audit = read_json(audit_path)
            if saved_audit["discovery_hash"] != digest(discovery):
                raise ValueError("Reviewed discovery changed; choose a new output directory")
            audit = saved_audit["review"]
            check_review(audit, input_path)
        else:
            audit = call_agent(harness, "review-lineage", REVIEW, review_payload, input_path, out,
                               "discovery-review", lambda r: check_review(r, input_path), args.attempts)
            write_json(audit_path, {"discovery_hash": digest(discovery), "review": audit})
        if not audit["accepted"]:
            raise ValueError("Discovery review rejected: " + "; ".join(audit["issues"]) +
                             ". Resolve context in a new run directory.")
        records = {}
        context_hash = digest(discovery)

        def trace_column(col):
            name = col["name"]
            id_ = f"{col['ordinal']:04d}-{digest(name)[:12]}"
            path = out / "columns" / f"{id_}.json"
            checker = lambda d: check_trace(d, input_path, args.schema, args.table, name)
            if path.exists():
                old = read_json(path)
                if old.get("context_hash") == context_hash and old["status"] == "complete_within_repo":
                    if not checker(old["trace"]):
                        raise ValueError("Checkpoint claims complete but has unresolved lineage")
                    check_review(old["review"], input_path)
                    if not old["review"]["accepted"]:
                        raise ValueError("Checkpoint has a rejected review")
                    return name, old
            task = dict(payload, discovery=discovery, column=col,
                        trace_reference=(ROOT / "skills/trace-column/references/semantics.md").read_text())
            record = {"status": "failed", "context_hash": context_hash}
            try:
                trace = call_agent(harness, "trace-column", TRACE, task, input_path, out,
                                   id_, checker, args.attempts)
                review = call_agent(harness, "review-lineage", REVIEW,
                                    dict(task, review_mode="column", trace=trace), input_path, out,
                                    id_ + "-review", lambda r: check_review(r, input_path), args.attempts)
                # One bounded semantic repair. A fresh reviewer checks the new result.
                if not review["accepted"]:
                    trace = call_agent(harness, "trace-column", TRACE,
                                       dict(task, prior_trace=trace, reviewer_issues=review["issues"]),
                                       input_path, out, id_ + "-repair", checker, args.attempts)
                    review = call_agent(harness, "review-lineage", REVIEW,
                                        dict(task, review_mode="column", trace=trace), input_path, out,
                                        id_ + "-rereview", lambda r: check_review(r, input_path), args.attempts)
                if review["accepted"]:
                    record.update(trace=trace, review=review,
                                  status="complete_within_repo" if checker(trace) else "partial",
                                  issues=trace["unresolved"])
                else:
                    record.update(status="review_rejected", issues=review["issues"], review=review)
                    write_json(out / "rejected" / f"{id_}.json", trace)
            except (ValueError, OSError, TimeoutError) as exc:
                record["issues"] = [str(exc)]
            write_json(path, record)
            print(f"{name}: {record['status']}", flush=True)
            return name, record

        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            futures = [pool.submit(trace_column, c) for c in discovery["columns"]]
            for future in as_completed(futures):
                name, record = future.result()
                records[name] = record
        return export(out, manifest, discovery, records)


def main():
    parser = argparse.ArgumentParser(description="Agentic lineage from a committed Git workspace")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo-url", required=True)
    parser.add_argument("--schema", required=True)
    parser.add_argument("--table", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--context", help="Text file: job/environment/config values and scope")
    parser.add_argument("--columns", help="Optional ordered JSON column list as independent coverage check")
    parser.add_argument("--model", default="sonnet")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--attempts", type=int, default=2)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--max-turns", type=int, default=60)
    parser.add_argument("--call-budget", type=float, default=2.0,
                        help="USD per invocation, not per run; see README for total exposure")
    parser.add_argument("--run-budget", type=float, default=100.0,
                        help="USD scheduling ceiling, including persisted reservations from earlier attempts")
    args = parser.parse_args()
    if any(not math.isfinite(v) or v <= 0 for v in (args.concurrency, args.attempts, args.timeout,
                                                   args.max_turns, args.call_budget, args.run_budget)):
        parser.error("Limits must be positive")
    try:
        report = run(args)
        print(json.dumps(report, indent=2))
        return 0 if report["complete_within_repo"] else 2
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        # Do not write into a directory owned by another run or overwrite its artifacts.
        print(f"Run stopped: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
