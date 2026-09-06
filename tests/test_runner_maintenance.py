"""Exercise maintenance ordering and startup failures without live services."""

import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "runner-upgrade"
DELETED_REGISTRATION = (
    "Failed to create a session. The runner registration has been deleted "
    "from the server, please re-configure."
)


class RunnerMaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="runner-maintenance-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.runner_root = self.root / "runners"
        self.runner_root.mkdir()
        (self.root / "plists").mkdir()

    def runner(self, name="runner-1", version="2.335.1"):
        directory = self.runner_root / name
        (directory / "bin").mkdir(parents=True)
        (directory / "_diag").mkdir()
        listener = directory / "bin/Runner.Listener"
        listener.write_text(f"#!/bin/bash\nprintf '%s\\n' '{version}'\n")
        listener.chmod(0o755)
        (directory / ".runner").write_text(json.dumps({
            "agentId": 1, "agentName": name,
            "gitHubUrl": "https://github.com/acme", "workFolder": "_work",
        }))
        (directory / ".credentials").write_text('{"scheme":"OAuth"}\n')
        (self.root / f"plists/com.github.{name}.plist").touch()
        return directory

    def shell(self, body, **env):
        definitions = SCRIPT.read_text().rsplit('main "$@"', 1)[0]
        harness = r'''
trap - EXIT
PROG_NAME=runner-upgrade
RUNNER_ROOT="$TEST_ROOT/runners"
LAUNCH_DAEMON_DIR="$TEST_ROOT/plists"
LOG_FILE="$RUNNER_ROOT/upgrade.log"
REAL_USER=fixture
REAL_GROUP=fixture
REAL_UID=501
REAL_HOME="$TEST_ROOT"
ASSUME_YES=1
HEALTH_TIMEOUT=6
event() { printf '%s\n' "$*" >> "$TEST_ROOT/events"; }
as_user() { "$@"; }
detect_arch() { :; }
ensure_sudo() { :; }
resolve_latest_release() { RUNNER_VERSION=2.337.0; }
sleep() { event sleep; }
ps() { :; }
sudo() {
  case "$1" in
    chown) return 0 ;;
    mkdir|touch|chmod|mv|rm|cp|ps|launchctl) "$@" ;;
    *) echo "Unexpected sudo: $*" >&2; return 95 ;;
  esac
}
launchctl() { echo "Unexpected launchctl: $*" >&2; return 96; }
'''
        return subprocess.run(
            ["/bin/bash", "-c", definitions + "\n" + harness + "\n" + body],
            env=dict(os.environ, TEST_ROOT=str(self.root), **env),
            text=True, capture_output=True, timeout=20,
        )

    def events(self):
        path = self.root / "events"
        return path.read_text().splitlines() if path.exists() else []

    def run_flow(self, **env):
        return self.shell(r'''
active_workers() {
  [[ "$1" != "${TEST_BUSY_RUNNER:-}" ]] || return 0
  return 1
}
download_tarball() {
  [[ "${TEST_BAD_ARCHIVE:-0}" != 1 ]] || { event archive-rejected; return 1; }
  event archive-verified
}
shutdown_runner() { event "shutdown $1"; }
start_runner() { event "start $1"; }
upgrade_runner() {
  event "upgrade $1"
  [[ "${TEST_FAIL_CANARY:-0}" != 1 || "$1" != runner-1 ]]
}
cmd_run
''', **env)

    def test_worker_detection_matches_only_target_executable(self):
        process_file = self.root / "processes"
        process_file.write_text("\n".join((
            "/sbin/launchd",
            str(self.runner_root / "runner-2/bin/Runner.Worker"),
            str(self.runner_root / "runner-1.prev/bin/Runner.Worker"),
            "/unrelated/runner-1/bin/Runner.Worker",
            "/usr/bin/tail",
        )) + "\n")
        result = self.shell(r'''
ps() { cat "$TEST_ROOT/processes"; }
if active_workers runner-1; then exit 42; else [[ $? == 1 ]]; fi
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        with process_file.open("a") as process_output:
            process_output.write(str(self.runner_root / "runner-1/bin/Runner.Worker") + "\n")
        result = self.shell(r'''
ps() { cat "$TEST_ROOT/processes"; }
active_workers runner-1
''')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_worker_inspection_failure_is_not_treated_as_idle(self):
        result = self.shell(r'''
ps() { return 1; }
if active_workers runner-1; then exit 42; else [[ $? == 2 ]]; fi
''')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_ambiguous_worker_executable_is_not_treated_as_idle(self):
        result = self.shell(r'''
ps() { printf '%s\n' Runner.Worker; }
if active_workers runner-1; then exit 42; else [[ $? == 2 ]]; fi
''')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_shutdown_preflights_entire_selection_before_stopping_any_runner(self):
        self.runner()
        self.runner("runner-2")
        result = self.shell(r'''
active_workers() { [[ "$1" == runner-2 ]]; }
bootout_system() { event "stop $1"; }
cmd_shutdown
''')
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.events(), result.stderr)

    def test_shutdown_refuses_when_worker_inspection_fails(self):
        self.runner()
        result = self.shell(r'''
active_workers() { return 2; }
bootout_system() { event "stop $1"; }
cmd_shutdown
''')
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.events(), result.stderr)

    def test_shutdown_rechecks_worker_before_stopping_service(self):
        self.runner()
        result = self.shell(r'''
active_workers() { return 0; }
bootout_system() { event "stop $1"; }
if shutdown_runner runner-1; then exit 42; fi
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.events(), result.stderr)

    def test_shutdown_single_target_leaves_other_runners_running(self):
        self.runner()
        self.runner("runner-2")
        result = self.shell(r'''
active_workers() { return 1; }
bootout_system() { event "stop $1"; }
parse_args shutdown --runner 2 --yes
cmd_shutdown
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.events(), ["stop com.github.runner-2"])
        self.assertTrue((self.runner_root / ".maintenance/runner-2").is_file())
        self.assertFalse((self.runner_root / ".maintenance/runner-1").exists())

    def test_nested_active_state_does_not_make_a_stopped_service_running(self):
        result = self.shell(r'''
launchctl() {
  printf '%s\n' 'state = not running' 'last exit code = 0' 'resource coalition = {' 'state = active' '}'
}
if service_is_running runner-1; then exit 42; fi
''')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_start_does_not_restart_an_already_running_service(self):
        self.runner()
        result = self.shell(r'''
service_is_running() { return 0; }
bootstrap_daemon() { event "bootstrap $1"; return 1; }
wait_for_listening() { event "health $1"; return 1; }
start_runner runner-1
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.events(), result.stderr)

    def test_start_bootstraps_stopped_service_and_checks_readiness(self):
        directory = self.runner()
        maintenance = self.runner_root / ".maintenance"
        maintenance.mkdir()
        marker = maintenance / "runner-1"
        marker.touch()
        result = self.shell(r'''
service_is_running() { return 1; }
bootstrap_daemon() { event "bootstrap $1"; }
wait_for_listening() { event "health $1"; }
start_runner runner-1
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.events(), ["bootstrap runner-1", f"health {directory}"])
        self.assertFalse(marker.exists())

    def test_start_refreshes_held_running_service_and_retains_hold_if_unhealthy(self):
        directory = self.runner()
        maintenance = self.runner_root / ".maintenance"
        maintenance.mkdir()
        marker = maintenance / "runner-1"
        marker.touch()
        result = self.shell(r'''
service_is_running() { return 0; }
bootstrap_daemon() { event "bootstrap $1"; }
wait_for_listening() { event "health $1"; HEALTH_FAILURE=registration_deleted; return 1; }
report_startup_failure() { :; }
if start_runner runner-1; then exit 42; fi
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.events(), ["bootstrap runner-1", f"health {directory}"])
        self.assertTrue(marker.is_file())

    def test_run_verifies_archive_then_stops_fleet_before_canary(self):
        self.runner()
        self.runner("runner-2")
        result = self.run_flow()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.events(), [
            "archive-verified", "shutdown runner-1", "shutdown runner-2",
            "upgrade runner-1", "upgrade runner-2",
        ])

    def test_canary_failure_leaves_remaining_installation_unmodified_and_stopped(self):
        self.runner()
        self.runner("runner-2")
        result = self.run_flow(TEST_FAIL_CANARY="1")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.events(), [
            "archive-verified", "shutdown runner-1", "shutdown runner-2", "upgrade runner-1",
        ])

    def test_run_with_busy_runner_does_not_stop_or_upgrade_any_runner(self):
        self.runner()
        self.runner("runner-2")
        result = self.run_flow(TEST_BUSY_RUNNER="runner-2")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.events(), result.stderr)

    def test_download_failure_leaves_fleet_running(self):
        self.runner()
        self.runner("runner-2")
        result = self.run_flow(TEST_BAD_ARCHIVE="1")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.events(), ["archive-rejected"])

    def test_run_starts_stopped_runner_already_on_current_version(self):
        self.runner(version="2.337.0")
        result = self.run_flow()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("start runner-1", self.events())
        self.assertNotIn("upgrade runner-1", self.events())

    def test_run_restarts_current_version_target_alongside_upgraded_canary(self):
        self.runner()
        self.runner("runner-2", version="2.337.0")
        result = self.run_flow()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.events(), [
            "archive-verified", "shutdown runner-1", "shutdown runner-2",
            "upgrade runner-1", "start runner-2",
        ])

    def test_deleted_registration_preflight_prevents_download_and_fleet_shutdown(self):
        self.runner()
        second = self.runner("runner-2")
        (second / "_diag/runner-stdout.log").write_text(DELETED_REGISTRATION + "\n")
        result = self.run_flow()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.events(), result.stderr)
        self.assertIn("repair", result.stderr.lower())

    def test_deleted_registration_fails_health_immediately_from_each_log_type(self):
        directory = self.runner()
        for filename in ("runner-stdout.log", "Runner_service.log"):
            with self.subTest(filename=filename):
                diagnostic = directory / "_diag" / filename
                diagnostic.write_text(DELETED_REGISTRATION + "\n")
                result = self.shell(r'''
if wait_for_listening "$RUNNER_ROOT/runner-1"; then exit 42; fi
[[ "$HEALTH_FAILURE" == registration_deleted ]]
''')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse(self.events(), result.stderr)
                diagnostic.unlink()

    def test_newer_version_query_log_does_not_hide_service_readiness(self):
        directory = self.runner()
        service_log = directory / "_diag/Runner_service.log"
        service_log.write_text("[INFO Terminal] WRITE LINE: Listening for Jobs\n")
        os.utime(service_log, (100, 100))
        version_log = directory / "_diag/Runner_version.log"
        version_log.write_text("[INFO Runner] Version: 2.337.0\n[INFO Runner] Command-line arguments: --version\n")
        os.utime(version_log, (200, 200))
        result = self.shell('wait_for_listening "$RUNNER_ROOT/runner-1"')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.events(), result.stderr)

    def test_health_timeout_accepts_positive_seconds(self):
        for arguments, expected in (("--health-timeout 60", "60"), ("--health-timeout=1", "1")):
            with self.subTest(arguments=arguments):
                result = self.shell(f'parse_args start --runner 1 {arguments}; printf "%s" "$HEALTH_TIMEOUT"')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, expected)

    def test_health_timeout_rejects_invalid_values_before_maintenance(self):
        for value in ("0", "-1", "abc", "1.5", ""):
            with self.subTest(value=value):
                result = self.shell('parse_args run --all --health-timeout ' + shlex.quote(value))
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertFalse(self.events(), result.stderr)


if __name__ == "__main__":
    unittest.main()
