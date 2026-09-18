"""Small adapter around the actual Claude Code headless CLI."""
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time


class Claude:
    def __init__(self, model, timeout, turns, budget, executable="claude"):
        self.model, self.timeout, self.turns, self.budget = model, timeout, turns, budget
        self.executable = executable

    def version(self):
        return subprocess.check_output([self.executable, "--version"], text=True).strip()

    def call(self, *, skill, schema, payload, snapshot, log):
        # Every task receives an isolated config home; no session history or target hooks.
        # Skills are injected explicitly, since bare mode disables auto-discovery.
        with tempfile.TemporaryDirectory(prefix="lineage-agent-") as home:
            env = os.environ.copy()
            env["CLAUDE_CONFIG_DIR"] = home
            env["DISABLE_AUTOUPDATER"] = "1"
            command = [self.executable, "--bare", "--restricted", "-p",
                       "--output-format", "json", "--json-schema", json.dumps(schema),
                       "--append-system-prompt-file", str(skill),
                       "--tools", "Read,Glob,Grep", "--allowedTools", "Read,Glob,Grep",
                       "--disallowedTools", "mcp__*", "--permission-mode", "dontAsk",
                       "--setting-sources", "", "--strict-mcp-config",
                       "--mcp-config", '{"mcpServers":{}}',
                       "--no-session-persistence", "--model", self.model,
                       "--max-turns", str(self.turns), "--max-budget-usd", str(self.budget)]
            started = time.monotonic()
            process = subprocess.Popen(command, cwd=snapshot, env=env, text=True,
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, start_new_session=True)
            try:
                stdout, stderr = process.communicate(json.dumps(payload), timeout=self.timeout)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                stdout, stderr = process.communicate()
                Path(str(log) + ".stdout").write_text(stdout)
                Path(str(log) + ".stderr").write_text(stderr)
                raise TimeoutError(f"Claude timed out after {self.timeout}s")
            except BaseException:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
                raise
            Path(str(log) + ".stdout").write_text(stdout)
            Path(str(log) + ".stderr").write_text(stderr)
            try:
                envelope = json.loads(stdout)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Claude emitted non-JSON output; see {log}.stdout") from exc
            if process.returncode or envelope.get("is_error") or envelope.get("subtype") != "success":
                raise ValueError(f"Claude failed ({envelope.get('subtype', process.returncode)}); see {log}.stdout")
            if "structured_output" not in envelope:
                raise ValueError("Claude returned no structured_output")
            metadata = {"seconds": round(time.monotonic() - started, 2),
                        "cost_usd": envelope.get("total_cost_usd"),
                        "session_id": envelope.get("session_id"), "model": self.model}
            return envelope["structured_output"], metadata
