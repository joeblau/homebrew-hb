"""Opt-in hardening tests for runner-ephemeral: sandbox-exec profiles and
APFS snapshot rollback. All tmutil/diskutil/sudo/rsync/sandbox-exec calls are
mocked; no real launchd, sudo, network, or /opt access."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]


def definitions(script):
    """Script body with its top-level main call removed, for sourcing."""
    text = (REPO / script).read_text()
    if '\nif [[ "${BASH_SOURCE[0]}"' in text:
        return text.rsplit('\nif [[ "${BASH_SOURCE[0]}"', 1)[0]
    return text.rsplit('main "$@"', 1)[0]


class SandboxProfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()

    def shell(self, body, **env):
        return subprocess.run(["/bin/bash", "-c", definitions("runner-ephemeral") + '\n' + body],
                              env=dict(os.environ, TEST_ROOT=str(self.root), **env),
                              text=True, capture_output=True)

    def test_profile_confines_writes_to_runner_tree(self):
        result = self.shell('generate_sandbox_profile "$TEST_ROOT/runner-1" 0')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("(deny file-write*)", result.stdout)
        self.assertIn(f'(subpath "{self.root}/runner-1")', result.stdout)
        self.assertIn('(subpath "/private/tmp")', result.stdout)
        self.assertIn('(literal "/dev/null")', result.stdout)
        self.assertNotIn("deny network", result.stdout)

    def test_profile_adds_operator_cache_paths(self):
        result = self.shell(
            'generate_sandbox_profile "$TEST_ROOT/runner-1" 0 '
            '"$TEST_ROOT/cache" "/opt/shared/toolchains"')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f'(allow file-write* (subpath "{self.root}/cache"))', result.stdout)
        self.assertIn('(allow file-write* (subpath "/opt/shared/toolchains"))', result.stdout)

    def test_profile_network_deny_variant(self):
        result = self.shell('generate_sandbox_profile "$TEST_ROOT/runner-1" 1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("(deny network*)", result.stdout)

    def test_sandboxed_cycle_runs_service_through_sandbox_exec(self):
        (self.root / "bin").mkdir()
        for name, code in (("config.sh", "touch .runner"), ("bin/runsvc.sh", "touch ran-marker")):
            path = self.root / name
            path.write_text("#!/bin/bash\n" + code + "\n")
            path.chmod(0o755)
        # PATH-based mock: exec() cannot dispatch to shell functions.
        mock_bin = self.root / "mockbin"
        mock_bin.mkdir()
        mock = mock_bin / "sandbox-exec"
        mock.write_text('#!/bin/bash\necho "sandbox-exec $*" >> "$TEST_ROOT/sandbox-calls"\n'
                        'shift 2   # drop -f PROFILE\nexec "$@"\n')
        mock.chmod(0o755)
        result = self.shell('''
DIR="$TEST_ROOT"; RUNNER_URL=https://github.com/acme
SANDBOX=1
SANDBOX_CACHE_PATHS=("$TEST_ROOT/cache")
fetch_registration_token() { FETCHED_TOKEN=secret; }
if ! run_cycle; then exit 99; fi
''', PATH=f"{mock_bin}:{os.environ['PATH']}")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = (self.root / "sandbox-calls").read_text()
        self.assertIn("./bin/runsvc.sh", calls)
        self.assertTrue((self.root / "ran-marker").exists())
        profile = (self.root / ".sandbox-exec.sb").read_text()
        self.assertIn("(deny file-write*)", profile)
        self.assertIn(f'(subpath "{self.root}")', profile)
        self.assertIn(f'(allow file-write* (subpath "{self.root}/cache"))', profile)

    def test_sandbox_prep_failure_fails_cycle_for_backoff(self):
        (self.root / "bin").mkdir()
        for name, code in (("config.sh", "touch .runner"), ("bin/runsvc.sh", "exit 0")):
            path = self.root / name
            path.write_text("#!/bin/bash\n" + code + "\n")
            path.chmod(0o755)
        result = self.shell('''
DIR="$TEST_ROOT"; RUNNER_URL=https://github.com/acme
SANDBOX=1
SANDBOX_PROFILE_FILE="$TEST_ROOT/missing-dir/profile.sb"
sandbox-exec() { :; }
fetch_registration_token() { FETCHED_TOKEN=secret; }
if run_cycle; then exit 99; fi
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root / "ran-marker").exists())


class SnapshotRollbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()

    def shell(self, body, **env):
        return subprocess.run(["/bin/bash", "-c", definitions("runner-ephemeral") + '\n' + body],
                              env=dict(os.environ, TEST_ROOT=str(self.root), **env),
                              text=True, capture_output=True)

    MOCKS = r'''
diskutil() { echo "   File System Personality:    APFS"; }
tmutil() {
  case "$1" in
    localsnapshot) echo "Created local snapshot with date: 2026-09-21-101010" ;;
    deletelocalsnapshot) echo "deleted $2" >> "$TEST_ROOT/deleted-snapshots" ;;
    *) exit 96 ;;
  esac
}
'''

    def test_snapshot_baseline_parsed_and_replaced(self):
        result = self.shell(self.MOCKS + '''
DIR="$TEST_ROOT"; SNAPSHOT_ROLLBACK=1
if ! take_workspace_snapshot; then exit 97; fi
first="$SNAPSHOT_NAME"
if ! take_workspace_snapshot; then exit 98; fi
printf '%s\n%s' "$first" "$SNAPSHOT_NAME"
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(),
                         ["com.apple.TimeMachine.2026-09-21-101010",
                          "com.apple.TimeMachine.2026-09-21-101010"])
        # The second take deleted the previous baseline before replacing it.
        self.assertEqual((self.root / "deleted-snapshots").read_text().splitlines(),
                         ["deleted 2026-09-21-101010"])

    def test_snapshot_restore_resets_workspace(self):
        snap_work = self.root / "snapview" / str(self.root).lstrip("/") / "_work"
        snap_work.mkdir(parents=True)
        (snap_work / "clean.txt").write_text("baseline")
        (self.root / "_work").mkdir()
        (self.root / "_work" / "stale.txt").write_text("leftover")
        result = self.shell(self.MOCKS + r'''
DIR="$TEST_ROOT"; SNAPSHOT_ROLLBACK=1
SNAPSHOT_NAME="com.apple.TimeMachine.2026-09-21-101010"; SNAPSHOT_DATE="2026-09-21-101010"
sudo() {
  [[ "$1" == "-n" ]] || exit 97
  shift
  case "$1" in
    /sbin/mount_apfs) cp -R "$TEST_ROOT/snapview/." "$6" ;;  # -r -s NAME DEV MNT
    umount) : ;;
    *) exit 98 ;;
  esac
}
rsync() {
  # mock: rsync -a --delete SRC/ DST/
  local src="" dst="" arg
  for arg in "$@"; do
    case "$arg" in -*) ;; *) if [[ -z "$src" ]]; then src="$arg"; else dst="$arg"; fi ;; esac
  done
  rm -rf "$dst"; mkdir -p "$dst"; cp -R "${src}." "$dst"
}
if ! wipe_state; then exit 99; fi
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / "_work" / "clean.txt").read_text(), "baseline")
        self.assertFalse((self.root / "_work" / "stale.txt").exists())

    def test_snapshot_restore_failure_falls_back_to_rm(self):
        (self.root / "_work").mkdir()
        (self.root / "_work" / "stale.txt").write_text("leftover")
        result = self.shell(self.MOCKS + '''
DIR="$TEST_ROOT"; SNAPSHOT_ROLLBACK=1
SNAPSHOT_NAME="com.apple.TimeMachine.2026-09-21-101010"; SNAPSHOT_DATE="2026-09-21-101010"
sudo() { return 1; }   # mount_apfs fails
if ! wipe_state; then exit 99; fi
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("falling back to rm -rf", result.stderr)
        self.assertFalse((self.root / "_work").exists())

    def test_non_apfs_volume_skips_snapshot_but_cycle_still_cleans(self):
        (self.root / "bin").mkdir()
        for name, code in (("config.sh", "touch .runner"), ("bin/runsvc.sh", "exit 0")):
            path = self.root / name
            path.write_text("#!/bin/bash\n" + code + "\n")
            path.chmod(0o755)
        (self.root / "_work").mkdir()
        (self.root / "_work" / "stale.txt").write_text("leftover")
        result = self.shell('''
DIR="$TEST_ROOT"; RUNNER_URL=https://github.com/acme; SNAPSHOT_ROLLBACK=1
diskutil() { echo "   Type (Bundle):            hfs"; }
tmutil() { echo "unexpected tmutil call" >&2; exit 96; }
fetch_registration_token() { FETCHED_TOKEN=secret; }
if ! run_cycle; then exit 99; fi
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("not on an APFS volume", result.stderr)
        self.assertFalse((self.root / "_work").exists())


class CliTests(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run(["/bin/bash", str(REPO / "runner-ephemeral"), *args],
                              text=True, capture_output=True)

    def test_help_lists_hardening_options(self):
        result = self.run_cli("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("USAGE:", result.stderr)
        self.assertIn("--sandbox", result.stderr)
        self.assertIn("--sandbox-deny-network", result.stderr)
        self.assertIn("--sandbox-cache-path", result.stderr)
        self.assertIn("--snapshot-rollback", result.stderr)

    def test_unknown_argument_is_usage_error(self):
        result = self.run_cli("--bogus")
        self.assertEqual(result.returncode, 2)
        self.assertIn("USAGE:", result.stderr)

    def test_sandbox_cache_path_requires_value(self):
        result = self.run_cli("--dir", "/tmp/x", "--org", "acme", "--sandbox-cache-path")
        self.assertEqual(result.returncode, 2)
        self.assertIn("USAGE:", result.stderr)


if __name__ == "__main__":
    unittest.main()
