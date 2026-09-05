"""Exercise runner upgrades with real files and archives, without sudo or launchd."""

import io
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "runner-upgrade"


class RunnerUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="runner-upgrade-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.runner_root = self.root / "runners"
        self.runner_root.mkdir()
        (self.root / "plists").mkdir()
        (self.root / "plists/com.github.runner-1.plist").touch()
        self.archive = self.root / "runner.tar.gz"
        with tarfile.open(self.archive, "w:gz") as archive:
            # Exercise executable-bit repair as well as the raw release layout,
            # whose diagnostic directory is created during configuration.
            for name, mode, contents in (
                ("bin/runsvc.sh", 0o644, "#!/bin/bash\nexit 0\n"),
                ("bin/Runner.Listener", 0o755, "#!/bin/bash\necho 2.337.0\n"),
            ):
                data = contents.encode()
                entry = tarfile.TarInfo(name)
                entry.mode = mode
                entry.size = len(data)
                archive.addfile(entry, io.BytesIO(data))

    def runner(self, name="runner-1", version="2.335.1", ready=False, workspace=True):
        directory = self.runner_root / name
        (directory / "bin").mkdir(parents=True)
        (directory / "_diag").mkdir()
        if workspace:
            (directory / "_work/project").mkdir(parents=True)
            (directory / "_work/project/artifact").write_text("valuable workspace")
        (directory / "bin/runsvc.sh").write_text("#!/bin/bash\nexit 0\n")
        (directory / "bin/runsvc.sh").chmod(0o755)
        (directory / "bin/Runner.Listener").write_text(f"#!/bin/bash\necho {version}\n")
        (directory / "bin/Runner.Listener").chmod(0o755)
        for filename in (
            ".runner", ".credentials", ".credentials_rsaparams", ".env", ".path",
            ".github_pat", ".service", ".runner_migrated", ".credentials_migrated",
        ):
            (directory / filename).write_text("fixture " + filename)
        (directory / ".github_pat").chmod(0o600)
        if ready:
            (directory / "_diag/Runner_latest.log").write_text("old process: Listening for Jobs\n")
        return directory

    def shell(self, body, **env):
        definitions = SCRIPT.read_text().rsplit('main "$@"', 1)[0]
        harness = r'''
trap - EXIT
RUNNER_ROOT="$TEST_ROOT/runners"
LAUNCH_DAEMON_DIR="$TEST_ROOT/plists"
LOG_FILE="$RUNNER_ROOT/upgrade.log"
TARBALL_PATH="$TEST_ROOT/runner.tar.gz"
RUNNER_VERSION=2.337.0
REAL_USER=fixture
REAL_GROUP=fixture
HEALTH_TIMEOUT=3
as_user() { "$@"; }
sleep() { :; }
xattr() { :; }
sudo() {
  case "$1" in
    chown) return 0 ;;
    launchctl|mv|mkdir|rm|cp|chmod|touch) "$@" ;;
    *) echo "Unexpected sudo: $*" >&2; return 95 ;;
  esac
}
launchctl() {
  printf '%s\n' "$*" >> "$TEST_ROOT/launchctl-commands"
  case "$1" in
    bootout)
      [[ "${TEST_STUCK:-0}" == 1 ]] || rm -f "$TEST_ROOT/loaded"
      ;;
    print)
      [[ "${TEST_STUCK:-0}" == 1 || -f "$TEST_ROOT/loaded" ]]
      ;;
    bootstrap)
      local dir="$RUNNER_ROOT/runner-1" version
      if [[ ! -x "$dir/bin/runsvc.sh" || ! -d "$dir/_diag" \
          || ! -f "$dir/_diag/runner-stdout.log" \
          || ! -f "$dir/_diag/runner-stderr.log" ]]; then
        echo 'Service not prepared: executable wrapper and log files required' >&2
        return 93
      fi
      touch "$TEST_ROOT/loaded"
      version="$("$dir/bin/Runner.Listener" --version)"
      printf 'stdout from %s\n' "$version" >> "$dir/_diag/runner-stdout.log"
      printf 'startup diagnostic from %s\n' "$version" >> "$dir/_diag/runner-stderr.log"
      if [[ "${TEST_READY_VERSION:-all}" == all || "${TEST_READY_VERSION:-all}" == "$version" ]]; then
        printf 'new process %s: Listening for Jobs\n' "$version" >> "$dir/_diag/Runner_latest.log"
      fi
      ;;
    enable) return 0 ;;
    kickstart) echo 'Unexpected redundant service restart' >&2; return 94 ;;
    *) echo "Unexpected launchctl: $*" >&2; return 96 ;;
  esac
}
'''
        return subprocess.run(
            ["/bin/bash", "-c", definitions + "\n" + harness + "\n" + body],
            env=dict(os.environ, TEST_ROOT=str(self.root), **env),
            text=True, capture_output=True, timeout=20,
        )

    def test_upgrade_prepares_raw_archive_for_launchd_and_preserves_state(self):
        directory = self.runner(ready=True)
        result = self.shell('if ! upgrade_runner runner-1; then exit 42; fi')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(subprocess.check_output(
            [str(directory / "bin/Runner.Listener"), "--version"], text=True,
        ).strip(), "2.337.0")
        for filename in (
            ".runner", ".credentials", ".credentials_rsaparams", ".env", ".path",
            ".github_pat", ".service", ".runner_migrated", ".credentials_migrated",
        ):
            self.assertEqual((directory / filename).read_text(), "fixture " + filename)
        self.assertEqual((directory / ".github_pat").stat().st_mode & 0o777, 0o600)
        self.assertEqual((directory / "_work/project/artifact").read_text(), "valuable workspace")
        self.assertFalse((self.runner_root / "runner-1.prev/_work").exists())
        self.assertTrue(os.access(directory / "bin/runsvc.sh", os.X_OK))
        self.assertNotIn("kickstart", (self.root / "launchctl-commands").read_text())
        self.assertNotIn("fixture .github_pat", result.stdout + result.stderr)

    def test_failed_upgrade_restores_workspace_and_retains_startup_diagnostics(self):
        directory = self.runner(ready=True)
        result = self.shell('if upgrade_runner runner-1; then exit 42; fi',
                            TEST_READY_VERSION="2.335.1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("rolled back and listening", result.stderr)
        self.assertEqual((directory / "_work/project/artifact").read_text(), "valuable workspace")
        self.assertEqual((directory / ".github_pat").read_text(), "fixture .github_pat")
        self.assertFalse((self.runner_root / "runner-1.prev").exists())
        retained = list((self.runner_root / ".upgrade-diagnostics").rglob("runner-stderr.log"))
        self.assertTrue(retained, result.stderr)
        self.assertTrue(any("startup diagnostic from 2.337.0" in path.read_text() for path in retained))
        self.assertEqual(subprocess.check_output(
            [str(directory / "bin/Runner.Listener"), "--version"], text=True,
        ).strip(), "2.335.1")

    def test_rollback_ignores_old_ready_log_and_reports_unconfirmed_health(self):
        self.runner(version="2.337.0")
        self.runner("runner-1.prev", ready=True, workspace=False)
        result = self.shell('if rollback_runner runner-1; then exit 42; fi',
                            TEST_READY_VERSION="none")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("rolled back and listening", result.stderr)
        self.assertIn("unconfirmed", (self.runner_root / "upgrade.log").read_text())
        retained = list((self.runner_root / ".upgrade-diagnostics").rglob("Runner_latest.log"))
        self.assertTrue(any("old process" in path.read_text() for path in retained))

    def test_rollback_accepts_fresh_ready_output_with_same_log_filename(self):
        self.runner(version="2.337.0")
        self.runner("runner-1.prev", ready=True, workspace=False)
        result = self.shell('if ! rollback_runner runner-1; then exit 42; fi')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("rolled back and listening", result.stderr)
        self.assertIn("new process 2.335.1", (self.runner_root / "runner-1/_diag/Runner_latest.log").read_text())

    def test_bootout_timeout_blocks_upgrade_before_files_are_moved(self):
        directory = self.runner()
        result = self.shell('if upgrade_runner runner-1; then exit 42; fi', TEST_STUCK="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.runner_root / "runner-1.prev").exists())
        self.assertEqual((directory / "_work/project/artifact").read_text(), "valuable workspace")
        self.assertIn("2.335.1", (directory / "bin/Runner.Listener").read_text())
        self.assertNotIn("bootstrap", (self.root / "launchctl-commands").read_text())

    def test_bootout_timeout_blocks_rollback_before_files_are_removed(self):
        current = self.runner(version="2.337.0")
        previous = self.runner("runner-1.prev", workspace=False)
        result = self.shell('if rollback_runner runner-1; then exit 42; fi', TEST_STUCK="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(previous.exists())
        self.assertIn("2.337.0", (current / "bin/Runner.Listener").read_text())
        self.assertEqual((current / "_work/project/artifact").read_text(), "valuable workspace")
        self.assertNotIn("bootstrap", (self.root / "launchctl-commands").read_text())

    def test_conflicting_workspaces_are_preserved_and_rollback_fails(self):
        current = self.runner(version="2.337.0")
        previous = self.runner("runner-1.prev")
        (previous / "_work/project/artifact").write_text("previous workspace")
        result = self.shell('if rollback_runner runner-1; then exit 42; fi')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((current / "_work/project/artifact").read_text(), "valuable workspace")
        self.assertEqual((previous / "_work/project/artifact").read_text(), "previous workspace")
        self.assertNotIn("bootstrap", (self.root / "launchctl-commands").read_text())

    def test_diagnostics_archive_failure_preserves_failed_install(self):
        current = self.runner(version="2.337.0")
        previous = self.runner("runner-1.prev", workspace=False)
        (current / "_diag/runner-stderr.log").write_text("valuable failure evidence")
        result = self.shell(r'''
as_user() {
  if [[ "$1" == mv && "$2" == */_diag ]]; then return 1; fi
  "$@"
}
if rollback_runner runner-1; then exit 42; fi
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((current / "_diag/runner-stderr.log").read_text(), "valuable failure evidence")
        self.assertTrue(previous.exists())
        self.assertEqual((previous / "_work/project/artifact").read_text(), "valuable workspace")
        self.assertNotIn("bootstrap", (self.root / "launchctl-commands").read_text())

    def test_bootstrap_does_not_continue_after_preparation_failure(self):
        self.runner()
        result = self.shell(r'''
as_user() { [[ "$1 $2" != 'chmod u+x' ]] || return 1; "$@"; }
if bootstrap_daemon runner-1; then exit 42; fi
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        commands = self.root / "launchctl-commands"
        if commands.exists():
            self.assertNotIn("bootstrap", commands.read_text())


if __name__ == "__main__":
    unittest.main()
