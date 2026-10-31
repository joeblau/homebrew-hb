"""runner-health restart recovery with fake launchctl; never touches launchd."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


BIN = Path(__file__).resolve().parents[1] / "bin"


class HealthRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="runner-health-recovery-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "plists").mkdir()
        (self.root / ".health").mkdir()
        (self.root / "runner-1").mkdir()
        (self.root / "plists/com.github.runner-1.plist").touch()

    def shell(self, launchctl, body="run_pass"):
        definitions = (BIN / "runner-health").read_text().rsplit('main "$@"', 1)[0]
        harness = r'''
trap - EXIT
RUNNER_ROOT="$TEST_ROOT"
LAUNCH_DAEMON_DIR="$TEST_ROOT/plists"
STATE_DIR="$TEST_ROOT/.health"
SUDO=""
HOST_LABEL=fixture
REMEDIATION_TIMEOUT=2
sleep() { :; }
trace() { printf '%s\n' "$*" >> "$TEST_ROOT/trace"; }
api_fetch() { API_ENABLED=0; }
alert() { trace "alert $1"; }
launchctl() {
  trace "launchctl $*"
''' + launchctl + r'''
}
'''
        return subprocess.run(
            ["/bin/bash", "-c", definitions + "\n" + harness + "\n" + body],
            env=dict(os.environ, TEST_ROOT=str(self.root)),
            text=True, capture_output=True, timeout=20,
        )

    def events(self):
        path = self.root / "trace"
        return path.read_text().splitlines() if path.exists() else []

    def test_stopped_runner_is_rebootstrapped_until_running(self):
        (self.root / "loaded").touch()
        result = self.shell(r'''
case "$1" in
  print)
    [[ -f "$TEST_ROOT/loaded" ]] || return 1
    cat "$TEST_ROOT/state" 2>/dev/null || echo 'state = not running'
    ;;
  bootout) rm -f "$TEST_ROOT/loaded" ;;
  bootstrap) touch "$TEST_ROOT/loaded"; echo 'state = running' > "$TEST_ROOT/state" ;;
  enable|kickstart) : ;;
esac
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('event="service_not_running" svc_state="running"', result.stdout)
        self.assertIn('action="rebootstrap" result="remediated"', result.stdout)
        self.assertIn("launchctl bootout system/com.github.runner-1", self.events())
        self.assertIn(
            f"launchctl bootstrap system {self.root}/plists/com.github.runner-1.plist",
            self.events(),
        )
        self.assertIn("launchctl kickstart -k system/com.github.runner-1", self.events())

    def test_loaded_but_stopped_after_restart_is_not_reported_as_remediated(self):
        result = self.shell(r'''
case "$1" in
  print) echo 'state = not running' ;;
  bootout|bootstrap|enable|kickstart) : ;;
esac
''')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('event="service_not_running" svc_state="not running"', result.stdout)
        self.assertIn('action="rebootstrap" result="failed"', result.stdout)
        self.assertIn('result="failed"', result.stdout)
        self.assertNotIn('result="remediated"', result.stdout)
        self.assertIn("alert runner-1", self.events())

    def test_restart_waits_for_a_delayed_running_state(self):
        result = self.shell(r'''
case "$1" in
  kickstart) : ;;
  print)
    count=$(cat "$TEST_ROOT/print-count" 2>/dev/null || echo 0)
    count=$((count + 1))
    echo "$count" > "$TEST_ROOT/print-count"
    if [[ "$count" -lt 3 ]]; then
      echo 'state = not running'
    else
      echo 'state = running'
    fi
    ;;
esac
''', 'remediate com.github.runner-1 "$TEST_ROOT/plists/com.github.runner-1.plist" kickstart')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / "print-count").read_text(), "3\n")
        self.assertEqual(self.events().count("launchctl print system/com.github.runner-1"), 3)


if __name__ == "__main__":
    unittest.main()
