"""Machine-readable output (--json / --dry-run) and the runner-mcp stdio server.

Everything runs against temp fixtures and fake sibling commands; no launchd,
Docker, MinIO, GitHub, or sudo access.
"""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_runner_docker_builder import MOCK as DOCKER_MOCK  # noqa: E402


REPO = Path(__file__).resolve().parents[1]


def definitions(script):
    """Script body with its top-level main call removed, for sourcing."""
    text = (REPO / script).read_text()
    if '\nif [[ "${BASH_SOURCE[0]}"' in text:
        return text.rsplit('\nif [[ "${BASH_SOURCE[0]}"', 1)[0]
    return text.rsplit('main "$@"', 1)[0]


class RunnerHealthJsonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="runner-health-json-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "plists").mkdir()
        (self.root / ".health").mkdir()

    def run_pass(self, dry_run):
        harness = r'''
trap - EXIT
RUNNER_ROOT="$TEST_ROOT"
LAUNCH_DAEMON_DIR="$TEST_ROOT/plists"
STATE_DIR="$TEST_ROOT/.health"
SUDO=""
HOST_LABEL=fixture
JSON=1
DRY_RUN=%d
trace() { printf '%%s\n' "$*" >> "$TEST_ROOT/trace"; }
launchctl() { trace "launchctl $*"; return 1; }
api_fetch() { API_ENABLED=0; }
alert() { trace "alert $1"; }
run_pass
''' % (1 if dry_run else 0)
        return subprocess.run(
            ["/bin/bash", "-c", definitions("runner-health") + "\n" + harness],
            env=dict(os.environ, TEST_ROOT=str(self.root)),
            text=True, capture_output=True, timeout=20,
        )

    def trace(self):
        path = self.root / "trace"
        return path.read_text() if path.exists() else ""

    def test_json_pass_is_one_document_with_a_record_per_runner(self):
        (self.root / "runner-1").mkdir()
        (self.root / "plists/com.github.runner-1.plist").touch()
        (self.root / "runner-2").mkdir()
        result = self.run_pass(dry_run=False)
        self.assertEqual(result.returncode, 1)
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1, result.stdout)
        doc = json.loads(lines[0])
        self.assertFalse(doc["ok"])
        self.assertEqual(doc["host"], "fixture")
        by_name = {r["runner"]: r for r in doc["runners"]}
        self.assertEqual(by_name["runner-1"]["event"], "daemon_not_loaded")
        self.assertEqual(by_name["runner-1"]["action"], "bootstrap")
        self.assertEqual(by_name["runner-1"]["result"], "failed")
        self.assertEqual(by_name["runner-2"]["event"], "daemon_missing")
        self.assertIn("launchctl bootstrap system", self.trace())
        self.assertIn("alert runner-1", self.trace())

    def test_dry_run_detects_without_remediation_or_alerts(self):
        (self.root / "runner-1").mkdir()
        (self.root / "plists/com.github.runner-1.plist").touch()
        (self.root / ".health/runner-1.alerted").touch()
        result = self.run_pass(dry_run=True)
        self.assertEqual(result.returncode, 1)
        doc = json.loads(result.stdout.strip())
        [record] = doc["runners"]
        self.assertEqual(record["result"], "detected")
        self.assertEqual(record["action"], "bootstrap")
        events = self.trace()
        self.assertNotIn("bootstrap system", events)
        self.assertNotIn("kickstart", events)
        self.assertNotIn("alert", events)
        # Alert suppression state is left untouched for the real watchdog.
        self.assertTrue((self.root / ".health/runner-1.alerted").exists())

    def test_no_runners_still_yields_a_document(self):
        result = self.run_pass(dry_run=True)
        self.assertEqual(result.returncode, 0)
        doc = json.loads(result.stdout.strip())
        self.assertEqual(doc, {**doc, "ok": True, "runners": []})


class RunnerLogsViewsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="runner-logs-views-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def runner(self, n, files):
        diag = self.root / f"runner-{n}" / "_diag"
        diag.mkdir(parents=True)
        for name, content in files.items():
            (diag / name).write_bytes(content)

    def logs(self, *args):
        harness = r'''
RUNNER_ROOT="$TEST_ROOT"
detect_platform() { :; }
main "$@"
'''
        return subprocess.run(
            ["/bin/bash", "-c", definitions("runner-logs") + "\n" + harness, "runner-logs", *args],
            env=dict(os.environ, TEST_ROOT=str(self.root)),
            text=True, capture_output=True, timeout=20,
        )

    def test_jobs_lists_completions_newest_first_as_json(self):
        self.runner(1, {"Worker_20260915-100100-utc.log": b"[WORKER] Job build completed with result: Succeeded\n"})
        self.runner(2, {"Worker_20260915-101000-utc.log": b"Job 'lint / lint' completed with result: Failed\n",
                        "Worker_20260915-090000-utc.log": b"still running\n"})
        result = self.logs("jobs", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        doc = json.loads(result.stdout)
        self.assertEqual([j["job"] for j in doc["jobs"]], ["lint / lint", "build"])
        self.assertEqual(doc["jobs"][0]["result"], "Failed")
        self.assertEqual(doc["jobs"][0]["started"], "2026-09-15T10:10:00Z")
        self.assertIsInstance(doc["jobs"][0]["duration_s"], int)
        limited = json.loads(self.logs("jobs", "--json", "--limit", "1", "--runner", "1").stdout)
        self.assertEqual([j["runner"] for j in limited["jobs"]], ["runner-1"])

    def test_tail_escapes_control_characters_and_reports_missing_logs(self):
        self.runner(2, {"Worker_20260915-101000-utc.log": b'a "quoted" \\ back\x1b[31mred\x1b[0m\ttab\r\nlast\n'})
        result = self.logs("tail", "--runner", "2", "--kind", "worker", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        doc = json.loads(result.stdout)
        self.assertEqual(doc["lines"], ['a "quoted" \\ back[31mred[0m\ttab', "last"])
        self.assertEqual(doc["kind"], "worker")
        self.assertTrue(doc["file"].endswith("Worker_20260915-101000-utc.log"))
        missing = json.loads(self.logs("tail", "--runner", "runner-2", "--kind", "stderr", "--json").stdout)
        self.assertEqual(missing, {"runner": "runner-2", "kind": "stderr", "file": None, "lines": []})
        plain = self.logs("tail", "--runner", "2", "--kind", "worker", "--lines", "1")
        self.assertEqual(plain.stdout, "last\n")

    def test_tail_requires_a_runner_choice_and_validates_input(self):
        self.runner(1, {})
        self.runner(2, {})
        self.assertEqual(self.logs("tail").returncode, 2)
        self.assertEqual(self.logs("tail", "--runner", "9").returncode, 1)
        self.assertEqual(self.logs("tail", "--runner", "x").returncode, 2)
        self.assertEqual(self.logs("tail", "--runner", "1", "--kind", "nope").returncode, 2)
        self.assertEqual(self.logs("jobs", "--limit", "0").returncode, 2)


class RunnerCacheStatusJsonTests(unittest.TestCase):
    def test_status_json_reports_state_without_prompts_or_secrets(self):
        with tempfile.TemporaryDirectory(prefix="runner-cache-json-") as temp:
            root = Path(temp)
            (root / "config").mkdir()
            (root / "data").mkdir()
            (root / "data/object").write_text("payload")
            (root / "config/minio.env").write_text(
                'MINIO_ROOT_USER="fixture-root"\nMINIO_ROOT_PASSWORD="fixture-secret"\n'
                'CACHE_PORT="19000"\nCACHE_CONSOLE_PORT="19001"\n'
            )
            prelude = r'''
source "$1"
CACHE_ROOT="$2"
MINIO_ENV_FILE="${CACHE_ROOT}/config/minio.env"
detect_platform() { :; }
detect_user() { :; }
require_cmds() { :; }
as_user() { "$@"; }
sudo() { [[ "$1" == "-n" ]] || { echo "interactive sudo" >&2; exit 90; }; return 1; }
curl() { return 7; }
mc() { printf '[2026-09-15 00:00:00 UTC]     0B actions-cache-repo-a/\n'; }
mc_admin() { mc; }
JSON=1
cmd_status
'''
            result = subprocess.run(
                ["/bin/bash", "-c", prelude, "test", str(REPO / "runner-cache"), str(root)],
                text=True, capture_output=True, timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            doc = json.loads(result.stdout)
            self.assertEqual(doc["installed"], True)
            self.assertEqual(doc["launchd_loaded"], False)
            self.assertEqual(doc["healthy"], False)
            self.assertEqual(doc["endpoint"], "http://127.0.0.1:19000")
            self.assertEqual(doc["buckets"], ["actions-cache-repo-a"])
            self.assertIsInstance(doc["disk_usage_kb"], int)
            self.assertNotIn("fixture-secret", result.stdout)


class DockerBuilderStatusJsonTests(unittest.TestCase):
    def test_status_json_lists_builders_and_selection(self):
        with tempfile.TemporaryDirectory(prefix="runner-builder-json-") as temp:
            root = Path(temp)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            for name in ("docker", "brew", "colima", "uname"):
                path = bin_dir / name
                path.write_text(f"#!{sys.executable}\n" + DOCKER_MOCK)
                path.chmod(0o755)
            (root / "state.json").write_text(json.dumps({
                "builders": {"runner-remote": {"driver": "docker-container", "endpoint": "ssh://ci@builder"}},
                "selected": "runner-remote", "colima_running": True,
            }))
            env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(root),
                   "DOCKER_CONFIG": str(root), "MOCK_ROOT": str(root)}
            result = subprocess.run(
                ["/bin/bash", str(REPO / "runner-docker-builder"), "status", "--json"],
                env=env, text=True, capture_output=True, timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            doc = json.loads(result.stdout)
            self.assertEqual(doc["selected_builder"], "runner-remote")
            self.assertEqual(doc["mode"], "remote")
            self.assertEqual(doc["builders"], [
                {"name": "runner-remote", "driver": "docker-container", "status": "", "selected": True},
            ])
            self.assertIsNone(doc["cache_total"])
            self.assertEqual(doc["colima"], {"installed": True, "running": True})
            calls = [json.loads(l) for l in (root / "calls.jsonl").read_text().splitlines()]
            self.assertFalse(any(c[:2] == ["brew", "install"] for c in calls))


FAKE_TOOL = r'''#!/bin/bash
printf '%s\n' "$(basename "$0") $*" >> "$RUNNER_MCP_TEST_CALLS"
if [[ -n "${FAKE_FAIL:-}" ]]; then echo "boom: $(basename "$0")" >&2; exit 3; fi
printf '{"tool":"%s","argv":"%s"}\n' "$(basename "$0")" "$*"
'''


class RunnerMcpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="runner-mcp-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for name in ("runner-health", "runner-logs", "runner-cache", "runner-docker-builder"):
            path = self.bin / name
            path.write_text(FAKE_TOOL)
            path.chmod(0o755)
        self.calls = self.root / "calls"

    def serve(self, *messages, env=None):
        stdin = "".join(json.dumps(m) + "\n" if isinstance(m, dict) else m + "\n" for m in messages)
        result = subprocess.run(
            ["/bin/bash", str(REPO / "runner-mcp")],
            input=stdin, text=True, capture_output=True, timeout=30,
            env={**os.environ, "RUNNER_MCP_BIN_DIR": str(self.bin),
                 "RUNNER_MCP_TEST_CALLS": str(self.calls), **(env or {})},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]

    def call(self, id, name, arguments=None):
        return {"jsonrpc": "2.0", "id": id, "method": "tools/call",
                "params": {"name": name, "arguments": arguments or {}}}

    def recorded(self):
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    def test_handshake_catalogue_and_notifications(self):
        replies = self.serve(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "ping"},
            {"jsonrpc": "2.0", "id": 4, "method": "resources/list"},
            "not json",
        )
        self.assertEqual([r["id"] for r in replies], [1, 2, 3, 4, None])
        self.assertEqual(replies[0]["result"]["protocolVersion"], "2025-03-26")
        self.assertEqual(replies[0]["result"]["capabilities"], {"tools": {}})
        names = [t["name"] for t in replies[1]["result"]["tools"]]
        self.assertEqual(names, ["runner_health", "runner_metrics", "runner_jobs", "runner_logs_tail",
                                 "cache_status", "docker_builder_status", "gh_failed_run_log"])
        for tool in replies[1]["result"]["tools"]:
            self.assertEqual(tool["inputSchema"]["type"], "object")
        self.assertEqual(replies[2]["result"], {})
        self.assertEqual(replies[3]["error"]["code"], -32601)
        self.assertEqual(replies[4]["error"]["code"], -32700)

    def test_tools_route_to_read_only_sibling_commands(self):
        replies = self.serve(
            self.call(1, "runner_health", {"repo": "acme/app", "stale_minutes": 30}),
            self.call(2, "runner_metrics"),
            self.call(3, "runner_jobs", {"runner": "2", "limit": 5}),
            self.call(4, "runner_logs_tail", {"runner": "runner-1", "kind": "worker", "lines": 40}),
            self.call(5, "cache_status"),
            self.call(6, "docker_builder_status"),
        )
        self.assertEqual(self.recorded(), [
            "runner-health --once --dry-run --json --repo acme/app --stale-minutes 30",
            "runner-logs metrics --once",
            "runner-logs jobs --json --runner 2 --limit 5",
            "runner-logs tail --json --runner runner-1 --kind worker --lines 40",
            "runner-cache status --json",
            "runner-docker-builder status --json",
        ])
        for reply in replies:
            self.assertFalse(reply["result"]["isError"], reply)
            payload = json.loads(reply["result"]["content"][0]["text"])
            self.assertIn("tool", payload)

    def test_invalid_arguments_never_reach_a_command(self):
        replies = self.serve(
            self.call(1, "runner_logs_tail", {"runner": "1; rm -rf /"}),
            self.call(2, "runner_logs_tail", {"kind": "../../etc/passwd"}),
            self.call(3, "runner_jobs", {"limit": 0}),
            self.call(4, "runner_health", {"org": "acme", "repo": "acme/app"}),
            self.call(5, "runner_health", {"repo": "acme/app/extra"}),
            self.call(6, "gh_failed_run_log", {"repo": "$(id)"}),
            self.call(7, "unknown_tool"),
        )
        self.assertEqual(self.recorded(), [])
        for reply in replies[:6]:
            self.assertTrue(reply["result"]["isError"], reply)
        self.assertEqual(replies[6]["error"]["code"], -32602)

    def test_command_failure_and_missing_command_are_tool_errors(self):
        (self.bin / "runner-cache").unlink()
        replies = self.serve(self.call(1, "runner_metrics"), self.call(2, "cache_status"), env={"FAKE_FAIL": "1"})
        self.assertTrue(replies[0]["result"]["isError"])
        self.assertIn("exited 3", replies[0]["result"]["content"][0]["text"])
        self.assertIn("boom: runner-logs", replies[0]["result"]["content"][0]["text"])
        self.assertTrue(replies[1]["result"]["isError"])
        self.assertIn("not installed", replies[1]["result"]["content"][0]["text"])

    def test_list_prints_catalogue_and_help_documents_usage(self):
        listing = subprocess.run(["/bin/bash", str(REPO / "runner-mcp"), "--list"], text=True, capture_output=True, timeout=20)
        self.assertEqual(listing.returncode, 0, listing.stderr)
        self.assertEqual(len(json.loads(listing.stdout)), 7)
        helptext = subprocess.run(["/bin/bash", str(REPO / "runner-mcp"), "--help"], text=True, capture_output=True, timeout=20)
        self.assertIn("USAGE", helptext.stderr)


if __name__ == "__main__":
    unittest.main()
