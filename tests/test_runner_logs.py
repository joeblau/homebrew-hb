"""runner-logs: step timelines, JUnit flake history, and the static dashboard.

All fixtures live in a temp directory; ps/launchctl/sudo and the real /opt
are never touched. The script is sourced so globals can be redirected at the
fixtures (the script's BASH_SOURCE guard keeps main from running on source).
"""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "runner-logs"

PRELUDE = r'''
source "$1"
trap - EXIT
TEST_ROOT="$2"
RUNNER_ROOT="${TEST_ROOT}/runners"
HOME="${TEST_ROOT}/home"
SAMPLES_FILE="${TEST_ROOT}/state/steps-samples.jsonl"
STEPS_STATE_FILE="${TEST_ROOT}/state/steps-state.tsv"
HISTORY_FILE="${TEST_ROOT}/state/test-history.jsonl"
DASHBOARD_OUTPUT="${TEST_ROOT}/state/dashboard.html"
OUTPUT_DIR="${TEST_ROOT}/out"
STATE_FILE="${TEST_ROOT}/state/offsets.tsv"
HOST_LABEL=test-host
detect_platform() { :; }
detect_host() { HOST_LABEL=test-host; }
mkdir -p "${RUNNER_ROOT}" "${HOME}" "${TEST_ROOT}/state"
'''

WORKER_LOG = """\
[2026-09-01 10:00:01Z INFO JobServerQueue] Job request 42 succeeded.
[2026-09-01 10:00:05Z INFO StepsRunner] Processing step: DisplayName='Checkout'
[2026-09-01 10:00:20Z INFO StepsRunner] Step result:  Succeeded
[2026-09-01 10:00:21Z INFO StepsRunner] Processing step: DisplayName='Run tests'
[2026-09-01 10:02:00Z INFO StepsRunner] Step result:  Failed
[2026-09-01 10:02:05Z INFO JobServerQueue] Job 'build' completed with result: Failed
"""

RUNNER_LOG = """\
[2026-09-01 09:59:00Z INFO Runner] GitHub Actions runner starting.
[2026-09-01 09:59:01Z INFO JobDispatcher] Listening for Jobs
"""

SAMPLES = """\
{"ts":"2026-09-01T10:00:10Z","runner":"runner-1","cpu_pct":10.0,"rss_mb":100}
{"ts":"2026-09-01T10:00:30Z","runner":"runner-1","cpu_pct":30.0,"rss_mb":200}
{"ts":"2026-09-01T10:01:00Z","runner":"runner-1","cpu_pct":50.0,"rss_mb":300}
{"ts":"2026-09-01T10:03:00Z","runner":"runner-2","cpu_pct":99.0,"rss_mb":999}
"""

JUNIT_RUN1 = """<testsuite name="unit" tests="2" failures="0" errors="0">
  <testcase classname="mod" name="test_a" time="0.1"/>
  <testcase classname="mod" name="test_b" time="0.2"/>
</testsuite>
"""

JUNIT_RUN2 = """<testsuite name="unit" tests="2" failures="1" errors="0">
  <testcase classname="mod" name="test_a" time="0.1"><failure>AssertionError</failure></testcase>
  <testcase classname="mod" name="test_b" time="0.2"/>
</testsuite>
"""


class RunnerLogsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="runner-logs-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        diag = self.root / "runners/runner-1/_diag"
        diag.mkdir(parents=True)
        (diag / "Worker_20260901-100000-utc.log").write_text(WORKER_LOG)
        (diag / "Runner_20260901-095900-utc.log").write_text(RUNNER_LOG)
        junit = self.root / "junit"
        junit.mkdir()
        (junit / "run1.xml").write_text(JUNIT_RUN1)
        (junit / "run2.xml").write_text(JUNIT_RUN2)

    def shell(self, body):
        return subprocess.run(
            ["/bin/bash", "-c", PRELUDE + "\n" + body, "test", str(SCRIPT), str(self.root)],
            text=True, capture_output=True, timeout=60)

    def write_samples(self):
        path = self.root / "state/steps-samples.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(SAMPLES)

    def history_lines(self):
        path = self.root / "state/test-history.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def ingest(self):
        return self.shell(f'main tests ingest --junit-glob "{self.root}/junit/*.xml"')

    # -- argument handling (no fixtures, subprocess against the real script) --

    def test_help_lists_new_modes(self):
        result = subprocess.run(["/bin/bash", str(SCRIPT), "--help"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn("USAGE:", result.stderr)
        for word in ("steps", "tests", "dashboard"):
            self.assertIn(word, result.stderr)

    def test_unknown_argument_exits_2(self):
        result = subprocess.run(["/bin/bash", str(SCRIPT), "bogus"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("USAGE:", result.stderr)

    def test_tests_mode_requires_subcommand(self):
        result = subprocess.run(["/bin/bash", str(SCRIPT), "tests"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("USAGE:", result.stderr)

    def test_tests_mode_rejects_bad_subcommand(self):
        result = subprocess.run(["/bin/bash", str(SCRIPT), "tests", "bogus"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)

    # -- steps mode --

    def test_steps_once_emits_per_step_timeline(self):
        self.write_samples()
        result = self.shell("main steps --once")
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(len(lines), 2)

        checkout, run_tests = lines
        self.assertEqual(checkout["runner"], "runner-1")
        self.assertEqual(checkout["job"], "build")
        self.assertEqual(checkout["job_result"], "Failed")
        self.assertEqual(checkout["step"], "Checkout")
        self.assertEqual(checkout["result"], "Succeeded")
        self.assertEqual(checkout["start"], "2026-09-01T10:00:05Z")
        self.assertEqual(checkout["end"], "2026-09-01T10:00:20Z")
        self.assertEqual(checkout["duration_s"], 15)
        self.assertEqual(checkout["samples"], 1)
        self.assertEqual(checkout["cpu_mean_pct"], 10.0)
        self.assertEqual(checkout["cpu_peak_pct"], 10.0)
        self.assertEqual(checkout["rss_peak_mb"], 100)

        self.assertEqual(run_tests["step"], "Run tests")
        self.assertEqual(run_tests["result"], "Failed")
        self.assertEqual(run_tests["duration_s"], 99)
        self.assertEqual(run_tests["samples"], 2)
        self.assertEqual(run_tests["cpu_mean_pct"], 40.0)
        self.assertEqual(run_tests["cpu_peak_pct"], 50.0)
        self.assertEqual(run_tests["rss_peak_mb"], 300)

    def test_steps_once_without_samples_still_emits_boundaries(self):
        result = self.shell("main steps --once")
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]["samples"], 0)
        self.assertIsNone(lines[0]["cpu_mean_pct"])
        self.assertIsNone(lines[0]["rss_peak_mb"])

    def test_steps_once_runner_filter(self):
        self.write_samples()
        result = self.shell("main steps --once --runner 2")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_steps_incremental_emit_is_not_duplicated(self):
        self.write_samples()
        result = self.shell("steps_emit incr; echo ---; steps_emit incr")
        self.assertEqual(result.returncode, 0, result.stderr)
        first, second = result.stdout.split("---\n")
        self.assertEqual(len(first.splitlines()), 2)
        self.assertEqual(second.strip(), "")
        state = (self.root / "state/steps-state.tsv").read_text()
        self.assertIn("Worker_20260901-100000-utc.log", state)

    def test_sample_runners_records_ps_stats(self):
        body = r'''
ps() {
  printf '%s\n' \
    " 5.0 102400 ${RUNNER_ROOT}/runner-1/bin/Runner.Worker" \
    "10.0 204800 ${RUNNER_ROOT}/runner-1/_work/repo/node index.js" \
    "99.0 999999 /unrelated/process"
}
sample_runners
'''
        result = self.shell(body)
        self.assertEqual(result.returncode, 0, result.stderr)
        samples = (self.root / "state/steps-samples.jsonl").read_text().splitlines()
        self.assertEqual(len(samples), 1)
        sample = json.loads(samples[0])
        self.assertEqual(sample["runner"], "runner-1")
        self.assertEqual(sample["cpu_pct"], 15.0)
        self.assertEqual(sample["rss_mb"], 300)

    # -- tests mode --

    def test_ingest_records_suites_and_dedupes(self):
        result = self.ingest()
        self.assertEqual(result.returncode, 0, result.stderr)
        records = self.history_lines()
        self.assertEqual(len(records), 2)
        first, second = records
        self.assertEqual(first["suite"], "unit")
        self.assertEqual(first["tests"], 2)
        self.assertEqual(first["failures"], 0)
        self.assertEqual(len(first["cases"]), 2)
        self.assertEqual(second["failures"], 1)
        failed = [c for c in second["cases"] if c["status"] == "failed"]
        self.assertEqual([c["name"] for c in failed], ["test_a"])

        again = self.ingest()
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(len(self.history_lines()), 2)
        self.assertEqual(again.stdout, "")

    def test_ingest_without_matches_fails(self):
        result = self.shell(f'main tests ingest --junit-glob "{self.root}/junit/nope-*.xml"')
        self.assertEqual(result.returncode, 1)
        self.assertIn("no JUnit XML files matched", result.stderr)

    def test_report_json_counts_and_flakes(self):
        self.assertEqual(self.ingest().returncode, 0)
        result = self.shell("main tests report --json")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(len(data["suites"]), 1)
        suite = data["suites"][0]
        self.assertEqual(suite["suite"], "unit")
        self.assertEqual(suite["runs"], 2)
        self.assertEqual(suite["passed"], 3)
        self.assertEqual(suite["failed"], 1)
        self.assertEqual(suite["errors"], 0)
        self.assertEqual(suite["skipped"], 0)
        self.assertEqual(len(data["flaky"]), 1)
        flake = data["flaky"][0]
        self.assertEqual(flake["test"], "mod.test_a")
        self.assertEqual(flake["flips"], 1)
        self.assertEqual(flake["last_status"], "failed")

    def test_report_text_lists_flaky_tests(self):
        self.assertEqual(self.ingest().returncode, 0)
        result = self.shell("main tests report")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("FLAKY TESTS", result.stdout)
        self.assertIn("mod.test_a", result.stdout)
        self.assertNotIn("mod.test_b ::", result.stdout)

    def test_report_without_history_fails(self):
        result = self.shell("main tests report")
        self.assertEqual(result.returncode, 1)
        self.assertIn("No test history", result.stderr)

    # -- dashboard mode --

    def test_dashboard_is_self_contained(self):
        self.assertEqual(self.ingest().returncode, 0)
        result = self.shell("main dashboard")
        self.assertEqual(result.returncode, 0, result.stderr)
        page = (self.root / "state/dashboard.html").read_text()
        self.assertIn("<html", page)
        self.assertIn("runner-1", page)
        self.assertIn("idle", page)
        self.assertIn("build", page)
        self.assertIn("Failed", page)
        self.assertIn("mod.test_a", page)
        self.assertIn("Top flaky tests", page)
        for external in ("http://", "https://", "src=", "href="):
            self.assertNotIn(external, page)

    def test_dashboard_output_override(self):
        out = self.root / "elsewhere/report.html"
        result = self.shell(f'main dashboard --output "{out}"')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(out.exists())
        self.assertIn("<html", out.read_text())


if __name__ == "__main__":
    unittest.main()
