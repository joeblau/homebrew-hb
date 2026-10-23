"""runner-health's console (GUI) session check, with fake launchctl/shutdown.

GUI jobs hang without the runner owner's gui/<uid> session; --require-gui
reports it missing and --reboot-for-gui restarts an idle host when automatic
login will recreate the session. Nothing here touches launchd or reboots.
"""
import getpass
import os
from pathlib import Path
import plistlib
import subprocess
import tempfile
import unittest

BIN = Path(__file__).resolve().parents[1] / "bin"
OWNER = getpass.getuser()   # a real account, so `id -u` resolves it


class HealthGuiSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="runner-health-gui-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / "plists").mkdir()
        (self.root / ".maintenance").mkdir()
        (self.root / ".health").mkdir()
        (self.root / "runner-1").mkdir()
        with (self.root / "plists/com.github.runner-1.plist").open("wb") as f:
            plistlib.dump({"Label": "com.github.runner-1", "UserName": OWNER,
                           "ProgramArguments": ["/x/runsvc.sh"]}, f)

    def shell(self, body, **env):
        definitions = (BIN / "runner-health").read_text().rsplit('main "$@"', 1)[0]
        harness = r'''
trap - EXIT
PROG_NAME=runner-health
RUNNER_ROOT="$TEST_ROOT"
LAUNCH_DAEMON_DIR="$TEST_ROOT/plists"
STATE_DIR="$TEST_ROOT/.health"
LOGINWINDOW_PLIST="$TEST_ROOT/loginwindow"
SUDO=""
HOST_LABEL=fixture
WORK_TMPDIR="$TEST_ROOT"
mkdir -p "$STATE_DIR"
trace() { printf '%s\n' "$*" >> "$TEST_ROOT/trace"; }
launchctl() {
  trace "launchctl $*"
  [[ "$1 $2" == "print gui/$(id -u)" && -f "$TEST_ROOT/gui-session" ]]
}
defaults() { cat "$TEST_ROOT/autologin" 2>/dev/null; }
console_owner() { cat "$TEST_ROOT/console-owner" 2>/dev/null || echo root; }
pgrep() { [[ -f "$TEST_ROOT/busy" ]]; }
shutdown() { trace "shutdown $*"; }
current_boot_uuid() { cat "$TEST_ROOT/boot" 2>/dev/null || echo BOOT-A; }
curl() { trace "UNEXPECTED curl"; return 91; }
api_fetch() { API_ENABLED=0; }
check_one() { emit_log "$1" check running 0 n/a false none ok; record_status "$1" OK; }
'''
        return subprocess.run(
            ["/bin/bash", "-c", definitions + "\n" + harness + "\n" + body],
            env=dict(os.environ, TEST_ROOT=str(self.root), **env),
            text=True, capture_output=True, timeout=20)

    def events(self):
        path = self.root / "trace"
        return path.read_text() if path.exists() else ""

    def auto_login(self, user=OWNER):
        (self.root / "autologin").write_text(user + "\n")

    def test_without_require_gui_nothing_is_checked(self):
        result = self.shell("run_pass")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("gui/", self.events())
        self.assertNotIn('runner="host"', result.stdout)

    def test_present_session_is_ok_and_clears_the_alert_marker(self):
        (self.root / "gui-session").touch()
        (self.root / ".health/host.alerted").write_text("1")
        result = self.shell("REQUIRE_GUI=1; run_pass")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('runner="host" event="check" svc_state="session"', result.stdout)
        self.assertFalse((self.root / ".health/host.alerted").exists())
        self.assertNotIn("shutdown", self.events())

    def test_missing_session_without_reboot_flag_fails_and_alerts_once(self):
        result = self.shell("REQUIRE_GUI=1; rc=0; run_pass || rc=$?; run_pass || true; exit $rc")
        self.assertEqual(result.returncode, 1)
        self.assertIn('runner="host" event="gui_session_missing" svc_state="no-session"', result.stdout)
        self.assertIn('action="none" result="failed"', result.stdout)
        self.assertIn("GUI jobs will fail here", result.stderr)
        self.assertTrue((self.root / ".health/host.alerted").exists())
        self.assertEqual(result.stderr.count("suppressed"), 1)
        self.assertNotIn("shutdown", self.events())

    def test_idle_host_with_auto_login_is_rebooted(self):
        self.auto_login()
        result = self.shell("parse_args --once --reboot-for-gui; run_pass")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("shutdown -r now runner-health: restoring the console session", self.events())
        self.assertIn('action="reboot" result="remediated"', result.stdout)
        # Runner records are written before the reboot is issued.
        self.assertLess(result.stdout.index('runner="runner-1"'), result.stdout.index('runner="host"'))
        self.assertEqual((self.root / ".health/host.rebooted_in").read_text(), "BOOT-A")

    def test_only_one_reboot_per_missing_session(self):
        self.auto_login()
        # Same boot as the marker: the reboot we issued has not happened yet.
        (self.root / ".health/host.rebooted_in").write_text("BOOT-A")
        result = self.shell("REBOOT_FOR_GUI=1; REQUIRE_GUI=1; run_pass")
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("shutdown", self.events())
        self.assertIn('action="reboot" result="deferred"', result.stdout)
        self.assertIn("already underway", result.stderr)
        # A new boot without a session: automatic login is not working.
        (self.root / "boot").write_text("BOOT-B")
        result = self.shell("REBOOT_FOR_GUI=1; REQUIRE_GUI=1; run_pass")
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("shutdown", self.events())
        self.assertIn('action="none" result="failed"', result.stdout)
        self.assertIn("not rebooting again", result.stderr)
        self.assertEqual((self.root / ".health/host.rebooted_in").read_text(), "BOOT-A")
        # A session clears the marker, so a later incident may reboot again.
        (self.root / "gui-session").touch()
        self.assertEqual(self.shell("REQUIRE_GUI=1; run_pass").returncode, 0)
        self.assertFalse((self.root / ".health/host.rebooted_in").exists())

    def test_reboot_is_deferred_while_a_job_runs(self):
        self.auto_login()
        (self.root / "busy").touch()
        result = self.shell("REBOOT_FOR_GUI=1; REQUIRE_GUI=1; run_pass")
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("shutdown", self.events())
        self.assertIn('action="reboot" result="deferred"', result.stdout)
        self.assertIn("reboot deferred until idle", result.stderr)

    def test_reboot_is_deferred_during_maintenance(self):
        self.auto_login()
        (self.root / ".maintenance/runner-1").touch()
        result = self.shell("REBOOT_FOR_GUI=1; REQUIRE_GUI=1; run_pass")
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("shutdown", self.events())
        self.assertIn("in maintenance, reboot deferred", result.stderr)

    def test_no_reboot_without_auto_login_for_the_owner(self):
        for user in (None, "someone-else"):
            with self.subTest(user=user):
                (self.root / "autologin").unlink(missing_ok=True)
                if user:
                    self.auto_login(user)
                result = self.shell("REBOOT_FOR_GUI=1; REQUIRE_GUI=1; run_pass")
                self.assertEqual(result.returncode, 1)
                self.assertNotIn("shutdown", self.events())
                self.assertIn("automatic login is not set", result.stderr)
                self.assertIn('action="none" result="failed"', result.stdout)

    def test_no_reboot_while_another_user_holds_the_console(self):
        self.auto_login()
        (self.root / "console-owner").write_text("operator\n")
        result = self.shell("REBOOT_FOR_GUI=1; REQUIRE_GUI=1; run_pass")
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("shutdown", self.events())
        self.assertIn("operator holds the console", result.stderr)

    def test_dry_run_detects_without_rebooting_or_alerting(self):
        self.auto_login()
        result = self.shell("DRY_RUN=1; ACT='would '; REBOOT_FOR_GUI=1; REQUIRE_GUI=1; run_pass")
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("shutdown", self.events())
        self.assertIn('action="reboot" result="detected"', result.stdout)
        self.assertFalse((self.root / ".health/host.alerted").exists())

    def test_json_pass_carries_the_host_record(self):
        result = self.shell("JSON=1; REQUIRE_GUI=1; run_pass")
        self.assertEqual(result.returncode, 1)
        import json
        doc = json.loads(result.stdout)
        self.assertFalse(doc["ok"])
        host = [r for r in doc["runners"] if r["runner"] == "host"]
        self.assertEqual(len(host), 1)
        self.assertEqual(host[0]["event"], "gui_session_missing")
        self.assertEqual(host[0]["svc_state"], "no-session")

    def test_owner_falls_back_to_the_runner_directory_owner(self):
        (self.root / "plists/com.github.runner-1.plist").write_bytes(b"")
        (self.root / "gui-session").touch()
        result = self.shell("REQUIRE_GUI=1; run_pass")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"console session gui/{os.getuid()} for {OWNER}", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
