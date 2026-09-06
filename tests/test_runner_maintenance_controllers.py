"""Maintenance holds must survive watchdog and autoscaler controller passes."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


REPO = Path(__file__).resolve().parents[1]


class MaintenanceControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="runner-maintenance-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / ".maintenance").mkdir()
        (self.root / ".autoscale").mkdir()
        (self.root / "plists").mkdir()
        self.trace = self.root / "trace"

    def hold(self, name="runner-1"):
        (self.root / ".maintenance" / name).touch()

    def runner(self, name="runner-1"):
        (self.root / name).mkdir()
        (self.root / "plists" / f"com.github.{name}.plist").touch()

    def shell(self, script, body):
        definitions = (REPO / script).read_text()
        if script == "runner-health":
            definitions = definitions.rsplit('main "$@"', 1)[0]
        else:
            definitions = definitions.rsplit('\nif [[ "${BASH_SOURCE[0]}"', 1)[0]
        harness = r'''
trap - EXIT
RUNNER_ROOT="$TEST_ROOT"
LAUNCH_DAEMON_DIR="$TEST_ROOT/plists"
STATE_DIR="$TEST_ROOT/.health"
SUDO=""
HOST_LABEL=fixture
trace() { printf '%s\n' "$*" >> "$TEST_ROOT/trace"; }
launchctl() { trace "launchctl $*"; return 1; }
curl() { trace "UNEXPECTED curl"; return 91; }
'''
        if script == "runner-health":
            harness += r'''
ensure_state_dir() { trace state-init; }
api_fetch() { trace api-fetch; API_ENABLED=0; }
alert() { trace "alert $1"; }
state_remove() { trace state-remove; }
'''
        else:
            harness += r'''
STATE_DIR="$TEST_ROOT/.autoscale"
parse_args --repo acme/repo --min 0 --max 4 --cooldown-minutes 0
log_decision() { trace "decision $3 $4"; }
validate_fleet() { trace validate; }
fetch_queue_depth() { trace queue-api; echo 1; }
api_list() { trace runners-api; echo '[]'; }
fetch_token() { trace "token-api $1"; echo fixture-token; }
provision_fixture() { trace provision; }
remove_fixture() { trace remove; }
RUNNER_SETUP_BIN=provision_fixture
RUNNER_CLEANUP_BIN=remove_fixture
'''
        return subprocess.run(
            ["/bin/bash", "-c", definitions + "\n" + harness + "\n" + body],
            env=dict(os.environ, TEST_ROOT=str(self.root)),
            text=True, capture_output=True, timeout=20,
        )

    def events(self):
        return self.trace.read_text() if self.trace.exists() else ""

    def test_health_skips_marked_runner_without_api_remediation_or_alerts(self):
        self.runner()
        self.hold()
        result = self.shell("runner-health", "run_pass")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.events(), "")
        self.assertIn('event="maintenance"', result.stdout)
        self.assertIn("MAINTENANCE (skipped)", result.stdout)
        self.assertFalse((self.root / ".health").exists())

    def test_health_only_checks_unmarked_runners_in_mixed_fleet(self):
        self.runner()
        self.runner("runner-2")
        self.hold()
        result = self.shell("runner-health", r'''
remediate() { trace "remediate $1"; return 0; }
run_pass
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("api-fetch", self.events())
        self.assertIn("remediate com.github.runner-2", self.events())
        self.assertIn("alert runner-2", self.events())
        self.assertNotIn("system/com.github.runner-1", self.events())
        self.assertNotIn("alert runner-1", self.events())

    def test_health_ignores_unrelated_markers_and_backup_directories(self):
        self.runner()
        self.runner("runner-1.prev")
        self.hold("runner-1.prev")
        result = self.shell("runner-health", r'''
remediate() { trace "remediate $1"; return 0; }
run_pass
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("remediate com.github.runner-1", self.events())
        self.assertNotIn("runner-1.prev", self.events() + result.stdout)

    def test_health_remediation_rechecks_maintenance_before_launchctl(self):
        self.hold()
        result = self.shell("runner-health", r'''
for action in bootstrap rebootstrap kickstart; do
  remediate com.github.runner-1 "$TEST_ROOT/plists/com.github.runner-1.plist" "$action"
done
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.events(), "")

    def test_health_suppresses_alert_when_maintenance_starts_during_check(self):
        self.runner()
        result = self.shell("runner-health", r'''
launchctl() { touch "$TEST_ROOT/.maintenance/runner-1"; return 1; }
check_one runner-1
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.events(), "")
        self.assertIn('event="maintenance"', result.stdout)

    def test_health_rechecks_hold_after_waiting_for_bootout(self):
        self.runner()
        result = self.shell("runner-health", r'''
bootout_system() { touch "$TEST_ROOT/.maintenance/runner-1"; }
launchctl() { trace "launchctl $*"; return 0; }
remediate com.github.runner-1 "$TEST_ROOT/plists/com.github.runner-1.plist" rebootstrap
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.root / ".maintenance/runner-1").is_file())
        self.assertEqual(self.events(), "")

    def test_health_does_not_kickstart_when_hold_starts_during_launchctl(self):
        self.runner()
        for action in ("bootstrap", "rebootstrap"):
            for held_during in ("bootstrap", "enable"):
                with self.subTest(action=action, held_during=held_during):
                    (self.root / ".maintenance/runner-1").unlink(missing_ok=True)
                    self.trace.unlink(missing_ok=True)
                    result = self.shell("runner-health", r'''
bootout_system() { :; }
launchctl() {
  trace "launchctl $*"
  if [[ "$1" == "''' + held_during + r'''" ]]; then
    touch "$TEST_ROOT/.maintenance/runner-1"
  fi
  return 0
}
remediate com.github.runner-1 "$TEST_ROOT/plists/com.github.runner-1.plist" ''' + action)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertTrue((self.root / ".maintenance/runner-1").is_file())
                    self.assertIn("launchctl bootstrap", self.events())
                    self.assertNotIn("kickstart", self.events())
                    self.assertNotIn("launchctl print", self.events())

    def test_autoscale_holds_even_when_marked_runner_directory_is_absent(self):
        self.hold()
        state = self.root / ".autoscale/state"
        state.write_text("IDLE_SINCE=123\n")
        result = self.shell("runner-autoscale", "run_tick")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("decision hold", self.events())
        self.assertNotIn("api", self.events())
        self.assertNotIn("validate", self.events())
        self.assertNotIn("provision", self.events())
        self.assertNotIn("remove", self.events())
        self.assertEqual(state.read_text(), "IDLE_SINCE=123\n")
        self.assertFalse((self.root / ".autoscale/lock").exists())

    def test_autoscale_direct_scale_operations_respect_hold(self):
        self.hold()
        result = self.shell("runner-autoscale", "do_scale_up 1 0\ndo_scale_down 0 1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.events().count("decision hold"), 2)
        self.assertNotIn("api", self.events())
        self.assertNotIn("provision", self.events())
        self.assertNotIn("remove", self.events())

    def test_autoscale_unrelated_names_and_marker_directories_do_not_hold(self):
        self.hold("runner-1.prev")
        self.hold("notes")
        (self.root / ".maintenance/runner-2").mkdir()
        result = self.shell("runner-autoscale", "run_tick")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("queue-api", self.events())
        self.assertIn("token-api registration-token", self.events())
        self.assertIn("provision", self.events())
        self.assertNotIn("decision hold", self.events())

    def test_autoscale_rechecks_hold_after_fetching_registration_token(self):
        result = self.shell("runner-autoscale", r'''
fetch_token() { touch "$TEST_ROOT/.maintenance/runner-1"; echo fixture-token; }
do_scale_up 1 0
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("decision hold", self.events())
        self.assertNotIn("provision", self.events())


if __name__ == "__main__":
    unittest.main()
