"""Autoscaling regressions using temporary fleets and a fixture-only API."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "runner-autoscale"


class AutoscaleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.responses = {}
        self.mock = self.root / "api.py"
        self.mock.write_text('''import json, pathlib, sys
root = pathlib.Path(__file__).parent
with (root / "requests").open("a") as f: f.write(sys.argv[2] + "\\n")
responses = json.loads((root / "responses.json").read_text())
value = responses.get(sys.argv[2])
if value is None: sys.exit(22)
print(json.dumps(value))
''')

    def runner(self, index, name=None, url="https://github.com/acme/repo"):
        folder = self.root / f"runner-{index}"
        folder.mkdir()
        (folder / ".runner").write_text(json.dumps({"agentName": name or f"mac-runner-{index}", "gitHubUrl": url}))

    def api(self, path, field, items, total=None):
        self.responses[path] = {"total_count": len(items) if total is None else total, field: items}

    def queue(self, queued=(), progressing=()):
        self.api("/repos/acme/repo/actions/runs?status=queued&per_page=100&page=1", "workflow_runs", list(queued))
        self.api("/repos/acme/repo/actions/runs?status=in_progress&per_page=100&page=1", "workflow_runs", list(progressing))

    def run_shell(self, commands, args="--repo acme/repo --min 0 --max 4 --cooldown-minutes 0"):
        (self.root / "responses.json").write_text(json.dumps(self.responses))
        q = shlex.quote
        body = f'''source {q(str(SCRIPT))}
RUNNER_ROOT={q(str(self.root))}
STATE_DIR="${{RUNNER_ROOT}}/.autoscale"
mkdir -p "${{STATE_DIR}}"
parse_args {args}
api_request() {{ python3 {q(str(self.mock))} "$@"; }}
log_decision() {{ printf '%s:%s\\n' "$3" "$4" >> "${{RUNNER_ROOT}}/decisions"; }}
{commands}
'''
        return subprocess.run(["/bin/bash", "-c", body], text=True, capture_output=True, env=os.environ.copy())

    def test_org_requires_explicit_queue_repositories(self):
        result = self.run_shell(":", args="--org acme")
        self.assertEqual(result.returncode, 2)
        self.assertIn("--queue-repo", result.stderr)

    def test_org_rejects_foreign_queue_repository(self):
        result = self.run_shell(":", args="--org acme --queue-repo elsewhere/repo")
        self.assertEqual(result.returncode, 2)

    def test_matrix_queue_and_case_insensitive_labels(self):
        self.queue(progressing=[{"id": 42}])
        self.api("/repos/acme/repo/actions/runs/42/jobs?filter=latest&per_page=100&page=1", "jobs", [
            {"id": 1, "status": "queued", "labels": ["self-hosted", "macOS"]},
            {"id": 2, "status": "queued", "labels": ["self-hosted", "linux"]},
            {"id": 3, "status": "in_progress", "labels": ["macos"]},
        ])
        result = self.run_shell("fetch_queue_depth")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "1")

    def test_paginated_queue_and_jobs_are_counted(self):
        self.queue(queued=[{"id": 40}])
        self.responses["/repos/acme/repo/actions/runs?status=queued&per_page=100&page=1"]["total_count"] = 2
        self.api("/repos/acme/repo/actions/runs?status=queued&per_page=100&page=2", "workflow_runs", [{"id": 41}], 2)
        for run in [40, 41]:
            self.api(f"/repos/acme/repo/actions/runs/{run}/jobs?filter=latest&per_page=100&page=1", "jobs", [{"id": run * 10, "status": "queued", "labels": ["macos"]}], 2)
            self.api(f"/repos/acme/repo/actions/runs/{run}/jobs?filter=latest&per_page=100&page=2", "jobs", [{"id": run * 10 + 1, "status": "queued", "labels": ["macos"]}], 2)
        result = self.run_shell("fetch_queue_depth")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "4")

    def test_incomplete_pagination_fails_closed(self):
        self.api("/x?per_page=100&page=1", "runners", [{"id": 1}], 2)
        self.api("/x?per_page=100&page=2", "runners", [], 2)
        self.assertNotEqual(self.run_shell("api_list /x runners").returncode, 0)

    def test_api_search_cap_fails_closed(self):
        self.api("/x?per_page=100&page=1", "workflow_runs", [], 1001)
        self.assertNotEqual(self.run_shell("api_list /x workflow_runs").returncode, 0)

    def test_noninteger_or_negative_total_count_fails_closed(self):
        for total in (-1, 0.5, 1e25):
            with self.subTest(total=total):
                self.api("/x?per_page=100&page=1", "runners", [], total)
                self.assertNotEqual(self.run_shell("api_list /x runners").returncode, 0)

    def test_noninteger_and_negative_ids_fail_closed(self):
        for run_id in (-1, 0.5, 0):
            with self.subTest(run_id=run_id):
                self.queue(queued=[{"id": run_id}])
                self.assertNotEqual(self.run_shell("fetch_queue_depth").returncode, 0)

    def test_threshold_must_be_positive(self):
        result = self.run_shell(":", args="--repo acme/repo --scale-up-threshold 0")
        self.assertEqual(result.returncode, 2)

    def test_ephemeral_flag_and_file_resolved_pat_reach_setup(self):
        fake = self.root / "setup"
        fake.write_text('#!/bin/bash\nprintf "%s|%s\\n" "$*" "$GITHUB_PAT"\n')
        fake.chmod(0o755)
        result = self.run_shell(f'fetch_token() {{ echo example; }}; GITHUB_PAT=resolved-from-file; RUNNER_SETUP_BIN={shlex.quote(str(fake))}; do_scale_up 1 0', args="--repo acme/repo --ephemeral")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--ephemeral", result.stdout)
        self.assertIn("resolved-from-file", result.stdout)

    def test_ephemeral_fleet_mode_must_match_controller(self):
        self.runner(1)
        result = self.run_shell("validate_fleet", args="--repo acme/repo --ephemeral")
        self.assertNotEqual(result.returncode, 0)
        marker = self.root / "runner-1/.runner"
        settings = json.loads(marker.read_text())
        settings["ephemeral"] = True
        marker.write_text(json.dumps(settings))
        result = self.run_shell("validate_fleet", args="--repo acme/repo --ephemeral")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_single_job_scales_from_zero(self):
        result = self.run_shell('fetch_queue_depth() { echo 1; }; do_scale_up() { echo scaled; }; run_tick')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "scaled")

    def test_minimum_is_restored_without_queued_jobs(self):
        self.api("/repos/acme/repo/actions/runners?per_page=100&page=1", "runners", [])
        result = self.run_shell('fetch_queue_depth() { echo 0; }; do_scale_up() { echo scaled; }; run_tick', args="--repo acme/repo --min 1 --max 4")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "scaled")

    def test_busy_identity_matches_exact_local_name(self):
        self.runner(1)
        rows = [{"name": "other-runner-1", "status": "online", "busy": True}, {"name": "mac-runner-1", "status": "online", "busy": False}]
        result = self.run_shell("all_runners_idle " + shlex.quote(json.dumps(rows, indent=2)))
        self.assertEqual(result.returncode, 0, result.stderr)
        rows[1]["busy"] = True
        self.assertNotEqual(self.run_shell("all_runners_idle " + shlex.quote(json.dumps(rows, indent=2))).returncode, 0)

    def test_missing_runner_is_not_idle(self):
        self.runner(1)
        self.assertNotEqual(self.run_shell("all_runners_idle '[]'").returncode, 0)

    def test_foreign_registration_blocks_tick(self):
        self.runner(1, url="https://github.com/other/repo")
        result = self.run_shell('fetch_queue_depth() { echo 1; }; do_scale_up() { echo unsafe; }; run_tick')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("unsafe", result.stdout)

    def test_numbering_gap_only_fills_one_runner(self):
        self.runner(1)
        self.runner(3)
        fake = self.root / "setup"
        fake.write_text('#!/bin/bash\nprintf "%s\\n" "$*"\n')
        fake.chmod(0o755)
        result = self.run_shell(f'fetch_token() {{ echo example; }}; RUNNER_SETUP_BIN={shlex.quote(str(fake))}; do_scale_up 1 2')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--runners 2", result.stdout)

    def test_failed_scale_releases_lock_and_returns_failure(self):
        result = self.run_shell('fetch_queue_depth() { echo 1; }; do_scale_up() { return 7; }; run_tick')
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertFalse((self.root / ".autoscale/lock").exists())

    def test_runner_list_failure_never_scales_down(self):
        self.runner(1)
        result = self.run_shell('fetch_queue_depth() { echo 0; }; do_scale_down() { echo unsafe; }; run_tick')
        self.assertNotIn("unsafe", result.stdout)

    def fake_setup(self, script='printf "%s\\n" "$*"\n'):
        fake = self.root / "setup"
        fake.write_text("#!/bin/bash\n" + script)
        fake.chmod(0o755)
        return fake

    def online_runners_api(self, rows):
        self.api("/repos/acme/repo/actions/runners?per_page=100&page=1", "runners", rows)

    def test_burst_provisions_bounded_batch_in_one_tick(self):
        self.queue(queued=[{"id": 40}])
        self.api("/repos/acme/repo/actions/runs/40/jobs?filter=latest&per_page=100&page=1", "jobs", [
            {"id": j, "status": "queued", "labels": ["macos"]} for j in (1, 2, 3)])
        self.online_runners_api([])
        fake = self.fake_setup()
        result = self.run_shell(
            f'fetch_token() {{ echo example; }}; RUNNER_SETUP_BIN={shlex.quote(str(fake))}; run_tick',
            args="--repo acme/repo --min 0 --max 6 --scale-up-batch 2 --cooldown-minutes 0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--runners 2", result.stdout)

    def test_scale_up_batch_must_be_positive(self):
        result = self.run_shell(":", args="--repo acme/repo --scale-up-batch 0")
        self.assertEqual(result.returncode, 2)

    def test_capacity_budget_below_min_rejected(self):
        result = self.run_shell(":", args="--repo acme/repo --min 3 --max 4 --capacity-budget 2")
        self.assertEqual(result.returncode, 2)
        self.assertIn("--capacity-budget", result.stderr)

    def test_idle_capacity_offsets_burst_demand(self):
        self.runner(1)
        self.online_runners_api([{"name": "mac-runner-1", "status": "online", "busy": False}])
        fake = self.fake_setup()
        result = self.run_shell(
            f'fetch_token() {{ echo example; }}; RUNNER_SETUP_BIN={shlex.quote(str(fake))}; do_scale_up 3 1',
            args="--repo acme/repo --min 0 --max 6 --scale-up-batch 5 --cooldown-minutes 0")
        self.assertEqual(result.returncode, 0, result.stderr)
        # Demand 3 minus 1 ready idle runner = 2 new runners (indices 2 and 3).
        self.assertIn("--runners 3", result.stdout)

    def test_idle_capacity_covering_demand_skips_scale_up(self):
        self.runner(1)
        self.online_runners_api([{"name": "mac-runner-1", "status": "online", "busy": False}])
        fake = self.fake_setup()
        result = self.run_shell(
            f'fetch_token() {{ echo example; }}; RUNNER_SETUP_BIN={shlex.quote(str(fake))}; do_scale_up 1 1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("--runners", result.stdout)
        self.assertIn("none", (self.root / "decisions").read_text())

    def test_unregistered_directory_is_not_ready_capacity(self):
        self.runner(1)
        self.online_runners_api([])  # registration gap: not in the API list
        fake = self.fake_setup()
        result = self.run_shell(
            f'fetch_token() {{ echo example; }}; RUNNER_SETUP_BIN={shlex.quote(str(fake))}; do_scale_up 2 1',
            args="--repo acme/repo --min 0 --max 6 --scale-up-batch 4 --cooldown-minutes 0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--runners 3", result.stdout)

    def test_busy_and_foreign_runners_do_not_offset_demand(self):
        self.runner(1)
        self.online_runners_api([
            {"name": "mac-runner-1", "status": "online", "busy": True},
            {"name": "other-mac-runner-1", "status": "online", "busy": False},
        ])
        fake = self.fake_setup()
        result = self.run_shell(
            f'fetch_token() {{ echo example; }}; RUNNER_SETUP_BIN={shlex.quote(str(fake))}; do_scale_up 2 1',
            args="--repo acme/repo --min 0 --max 6 --scale-up-batch 4 --cooldown-minutes 0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--runners 3", result.stdout)

    def test_batch_is_clamped_by_max_and_capacity_budget(self):
        for i in (1, 2, 3):
            self.runner(i)
        self.online_runners_api([])
        fake = self.fake_setup()
        quoted = shlex.quote(str(fake))
        # Room below --max 4 is one runner even with depth 5 and batch 5.
        result = self.run_shell(
            f'fetch_token() {{ echo example; }}; RUNNER_SETUP_BIN={quoted}; do_scale_up 5 3',
            args="--repo acme/repo --min 0 --max 4 --scale-up-batch 5 --cooldown-minutes 0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--runners 4", result.stdout)
        # A per-host budget below --max wins: fleet of 2 at budget 2 adds nothing.
        result = self.run_shell(
            f'fetch_token() {{ echo example; }}; RUNNER_SETUP_BIN={quoted}; do_scale_up 5 2',
            args="--repo acme/repo --min 0 --max 6 --scale-up-batch 5 --capacity-budget 2 --cooldown-minutes 0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("--runners", result.stdout)

    def test_minimum_reconciled_in_one_batch_across_numbering_gaps(self):
        self.runner(1)
        self.runner(3)
        fake = self.fake_setup()
        result = self.run_shell(
            f'fetch_token() {{ echo example; }}; RUNNER_SETUP_BIN={shlex.quote(str(fake))}; do_scale_up 0 2',
            args="--repo acme/repo --min 4 --max 6 --scale-up-batch 4 --cooldown-minutes 0")
        self.assertEqual(result.returncode, 0, result.stderr)
        # Deficit is 2; targeting index 4 makes runner-setup fill gaps 2 and 4.
        self.assertIn("--runners 4", result.stdout)

    def test_runner_list_failure_limits_batch_to_one(self):
        fake = self.fake_setup()  # runner-list endpoint deliberately unregistered
        result = self.run_shell(
            f'fetch_token() {{ echo example; }}; RUNNER_SETUP_BIN={shlex.quote(str(fake))}; do_scale_up 5 0',
            args="--repo acme/repo --min 0 --max 8 --scale-up-batch 4 --cooldown-minutes 0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--runners 1", result.stdout)

    def test_partial_provisioning_failure_logs_requested_attempted_actual(self):
        fake = self.fake_setup(f'mkdir -p {shlex.quote(str(self.root))}/runner-1\nexit 1\n')
        result = self.run_shell(
            f'fetch_token() {{ echo example; }}; RUNNER_SETUP_BIN={shlex.quote(str(fake))}; do_scale_up 0 0',
            args="--repo acme/repo --min 2 --max 6 --scale-up-batch 2 --cooldown-minutes 0")
        self.assertNotEqual(result.returncode, 0)
        decisions = (self.root / "decisions").read_text()
        self.assertIn("error", decisions)
        self.assertIn("requested=2 attempted=2 actual=1", decisions)


if __name__ == "__main__":
    unittest.main()
