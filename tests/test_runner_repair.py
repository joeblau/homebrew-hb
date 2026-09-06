"""Re-register one runner with native listener mocks; never use GitHub or launchd."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "runner-upgrade"


class RunnerRepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="runner-repair-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.runner = self.root / "runners/runner-1"
        (self.runner / "bin").mkdir(parents=True)
        (self.runner / "_work/project").mkdir(parents=True)
        (self.runner / "_diag").mkdir()
        (self.runner / "_work/project/output").write_text("keep workspace")
        (self.runner / "_diag/Runner_old.log").write_text("registration was deleted")
        self.settings = {
            "agentId": 42, "agentName": "Mini01-runner-1",
            "gitHubUrl": "https://github.com/acme", "workFolder": "_work",
            "poolName": "Build Macs", "ephemeral": False, "disableUpdate": True,
        }
        (self.runner / ".runner").write_text(json.dumps(self.settings))
        for name in (".credentials", ".credentials_migrated", ".credentials_rsaparams",
                     ".env", ".path", ".service"):
            (self.runner / name).write_text("original " + name)
        (self.runner / ".runner_migrated").write_text(json.dumps(self.settings))
        (self.root / "plists").mkdir()
        (self.root / "plists/com.github.runner-1.plist").touch()
        (self.root / "runners/runner-2").mkdir()
        (self.root / "runners/runner-2/untouched").write_text("other runner")
        listener = self.runner / "bin/Runner.Listener"
        listener.write_text("#!" + sys.executable + "\n" + r'''
import json, os
from pathlib import Path
import sys

root = Path(os.environ["TEST_ROOT"])
directory = Path.cwd()
args = sys.argv[1:]
with (root / "listener-calls").open("a") as log:
    log.write(args[0] + "\n")
if args == ["remove", "--local"]:
    if os.environ.get("TEST_REMOVE_FAIL"):
        sys.exit(1)
    for name in (".runner", ".runner_migrated", ".credentials",
                 ".credentials_migrated", ".credentials_rsaparams"):
        (directory / name).unlink(missing_ok=True)
    sys.exit(0)
assert args[0] == "configure", args
assert "--replace" not in args, args
assert not (directory / ".runner").exists()
assert not (directory / ".runner_migrated").exists()
(root / "configure-args.json").write_text(json.dumps(args))
(directory / "_diag").mkdir(exist_ok=True)
(directory / "_diag/Runner_configure.log").write_text("new registration diagnostic")
if os.environ.get("TEST_CONFIG_FAIL"):
    print("Registration rejected", file=sys.stderr)
    sys.exit(1)
def value(name):
    return args[args.index(name) + 1]
settings = {
    "agentId": 84, "agentName": value("--name"), "gitHubUrl": value("--url"),
    "workFolder": value("--work"), "poolName": value("--runnergroup"),
    "ephemeral": "--ephemeral" in args, "disableUpdate": "--disableupdate" in args,
}
if os.environ.get("TEST_BAD_ARTIFACT"):
    settings["agentName"] = "wrong-runner"
(directory / ".runner").write_text(json.dumps(settings))
(directory / ".credentials").write_text("fresh credentials")
(directory / ".credentials_rsaparams").write_text("fresh RSA key")
''')
        listener.chmod(0o755)

    def shell(self, body="cmd_repair", **env):
        definitions = SCRIPT.read_text().rsplit('main "$@"', 1)[0]
        harness = r'''
trap - EXIT
RUNNER_ROOT="$TEST_ROOT/runners"
LAUNCH_DAEMON_DIR="$TEST_ROOT/plists"
LOG_FILE="$RUNNER_ROOT/upgrade.log"
TARGET_RUNNER=1
ASSUME_YES=1
REPAIR_LABELS="${TEST_LABELS-macos,macmini,self-hosted}"
REPAIR_LABELS_SET="${TEST_LABELS_SET-1}"
REPAIR_RUNNER_GROUP="${TEST_RUNNER_GROUP-}"
as_user() { "$@"; }
ensure_sudo() { :; }
prepare_event_log() { touch "$LOG_FILE"; }
shutdown_runner() {
  printf '%s\n' "$1" >> "$TEST_ROOT/stopped"
  [[ "${TEST_STOP_FAIL-}" != 1 ]]
}
start_runner() {
  printf '%s\n' "$1" >> "$TEST_ROOT/started"
  [[ "${TEST_START_FAIL-}" != 1 ]]
}
'''
        return subprocess.run(
            ["/bin/bash", "-c", definitions + "\n" + harness + "\n" + body],
            env={**os.environ, "TEST_ROOT": str(self.root), "RUNNER_TOKEN": "test-private-token", **env},
            text=True, capture_output=True, timeout=15,
        )

    def test_repair_preserves_settings_workspace_and_other_runner(self):
        result = self.shell()
        self.assertEqual(result.returncode, 0, result.stderr)
        actual = json.loads((self.runner / ".runner").read_text())
        self.assertEqual(actual, dict(self.settings, agentId=84))
        args = json.loads((self.root / "configure-args.json").read_text())
        self.assertEqual(args[args.index("--labels") + 1], "macos,macmini,self-hosted")
        self.assertNotIn("--replace", args)
        for name in (".env", ".path", ".service"):
            self.assertEqual((self.runner / name).read_text(), "original " + name)
        self.assertEqual((self.runner / "_work/project/output").read_text(), "keep workspace")
        self.assertEqual((self.root / "runners/runner-2/untouched").read_text(), "other runner")
        self.assertEqual((self.root / "stopped").read_text(), "runner-1\n")
        self.assertEqual((self.root / "started").read_text(), "runner-1\n")
        self.assertNotIn("test-private-token", result.stdout + result.stderr)
        backups = list((self.root / "runners/.registration-backups").glob("runner-1-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].stat().st_mode & 0o777, 0o700)
        self.assertEqual((backups[0] / ".credentials").read_text(), "original .credentials")

    def test_malformed_identity_is_rejected_before_shutdown(self):
        for change in ({"agentName": ""}, {"gitHubUrl": ""}, {"gitHubUrl": "https://"}, {"workFolder": "line\nbreak"},
                       {"ephemeral": "false"}, {"disableUpdate": 1}):
            with self.subTest(change=change):
                (self.runner / ".runner").write_text(json.dumps(dict(self.settings, **change)))
                result = self.shell()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Invalid registration settings", result.stderr)
                self.assertFalse((self.root / "stopped").exists())
                self.assertFalse((self.root / "listener-calls").exists())

    def test_multiple_settings_objects_are_rejected_before_shutdown(self):
        (self.runner / ".runner").write_text(json.dumps(self.settings) + json.dumps(self.settings))
        result = self.shell()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / "stopped").exists())

    def test_failed_local_removal_does_not_attempt_configuration(self):
        result = self.shell(TEST_REMOVE_FAIL="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.root / "listener-calls").read_text(), "remove\n")
        self.assertFalse((self.root / "started").exists())
        self.assertEqual(json.loads((self.runner / ".runner").read_text()), self.settings)

    def test_symlink_backup_pointer_is_rejected_before_shutdown(self):
        backups = self.root / "runners/.registration-backups"
        backups.mkdir()
        (backups / "runner-1.latest").symlink_to(self.runner / ".runner")
        result = self.shell()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must not be symlinks", result.stderr)
        self.assertFalse((self.root / "stopped").exists())

    def test_startup_failure_keeps_new_registration_without_binary_rollback(self):
        result = self.shell(TEST_START_FAIL="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads((self.runner / ".runner").read_text())["agentId"], 84)
        self.assertIn("startup health is unconfirmed", result.stderr)

    def test_noninteractive_requires_token_and_explicit_labels(self):
        for overrides in ({"RUNNER_TOKEN": ""}, {"TEST_LABELS_SET": "0"}):
            with self.subTest(overrides=overrides):
                result = self.shell(**overrides)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertFalse((self.root / "stopped").exists())

    def test_explicit_empty_labels_and_group_override_are_preserved(self):
        result = self.shell(TEST_LABELS="", TEST_RUNNER_GROUP="New Group")
        self.assertEqual(result.returncode, 0, result.stderr)
        args = json.loads((self.root / "configure-args.json").read_text())
        self.assertEqual(args[args.index("--labels") + 1], "")
        self.assertEqual(args[args.index("--runnergroup") + 1], "New Group")

    def test_stop_failure_leaves_registration_untouched(self):
        result = self.shell(TEST_STOP_FAIL="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads((self.runner / ".runner").read_text()), self.settings)
        self.assertFalse((self.root / "listener-calls").exists())

    def test_failed_registration_stays_stopped_preserves_logs_and_can_retry(self):
        result = self.shell(TEST_CONFIG_FAIL="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("remains stopped", result.stderr)
        self.assertFalse((self.root / "started").exists())
        self.assertFalse((self.runner / ".runner").exists())
        self.assertFalse((self.runner / ".credentials").exists())
        diagnostics = list((self.root / "runners/.upgrade-diagnostics").rglob("Runner_configure.log"))
        self.assertEqual(len(diagnostics), 1)
        self.assertEqual(diagnostics[0].read_text(), "new registration diagnostic")
        retry = self.shell()
        self.assertEqual(retry.returncode, 0, retry.stderr)
        self.assertIn("Recovering registration settings", retry.stderr)
        self.assertEqual(json.loads((self.runner / ".runner").read_text())["agentName"], self.settings["agentName"])

    def test_backup_pointer_outside_archive_is_rejected_before_shutdown(self):
        (self.runner / ".runner").unlink()
        (self.runner / ".runner_migrated").unlink()
        backups = self.root / "runners/.registration-backups"
        backups.mkdir()
        (backups / "runner-1.latest").write_text("runner-1-../../outside")
        result = self.shell()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Invalid repair backup pointer", result.stderr)
        self.assertFalse((self.root / "stopped").exists())

    def test_invalid_new_registration_never_starts_service(self):
        result = self.shell(TEST_BAD_ARTIFACT="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / "started").exists())
        self.assertIn("Registration failed", result.stderr)
        self.assertFalse((self.runner / ".runner").exists())
        failed = list((self.root / "runners/.registration-backups").rglob("failed-registration/.runner"))
        self.assertEqual(len(failed), 1)
        self.assertEqual(json.loads(failed[0].read_text())["agentName"], "wrong-runner")
        retry = self.shell()
        self.assertEqual(retry.returncode, 0, retry.stderr)
        self.assertEqual(json.loads((self.runner / ".runner").read_text())["agentName"], self.settings["agentName"])

    def test_xtrace_does_not_disclose_supplied_token(self):
        result = self.shell("set -x; cmd_repair")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("test-private-token", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
