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


if __name__ == "__main__":
    unittest.main()
