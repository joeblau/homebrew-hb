"""Persistent tool cache regression tests (issue #53).

The GitHub runner resolves its tool cache from the first of
RUNNER_TOOL_CACHE / RUNNER_TOOLSDIRECTORY / AGENT_TOOLSDIRECTORY and
otherwise uses _work/_tool (verified against actions/runner
src/Runner.Common/HostContext.cs, WellKnownDirectory.Tools; JobRunner.cs
creates the directory and exposes it to job steps as RUNNER_TOOL_CACHE,
which the setup-* actions consume). These tests exercise OUR wiring of that
contract — config.sh/runsvc.sh/sudo/launchctl/plutil are mocked fixtures, so
there is no real runner binary, network, launchd, sudo, or /opt access.
"""
import io
import json
import os
from pathlib import Path
import plistlib
import subprocess
import tarfile
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]


def definitions(script):
    """Script body with its top-level main call removed, for sourcing."""
    text = (REPO / script).read_text()
    if '\nif [[ "${BASH_SOURCE[0]}"' in text:
        return text.rsplit('\nif [[ "${BASH_SOURCE[0]}"', 1)[0]
    return text.rsplit('main "$@"', 1)[0]


class EphemeralToolCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()

    def shell(self, body, **env):
        return subprocess.run(
            ["/bin/bash", "-c", definitions("runner-ephemeral") + '\ntrap - EXIT\n' + body],
            env=dict(os.environ, TEST_ROOT=str(self.root), **env),
            text=True, capture_output=True)

    def make_runner(self, config_code="touch .runner", runsvc_code="exit 0"):
        (self.root / "bin").mkdir(exist_ok=True)
        for name, code in (("config.sh", config_code), ("bin/runsvc.sh", runsvc_code)):
            path = self.root / name
            path.write_text("#!/bin/bash\n" + code + "\n")
            path.chmod(0o755)

    def test_wipe_preserves_tool_cache_but_resets_work_and_registration(self):
        (self.root / "_toolcache/node/20.11.0").mkdir(parents=True)
        (self.root / "_toolcache/node/20.11.0/installed").write_text("cached tool")
        (self.root / "_work/checkout").mkdir(parents=True)
        for name in (".env", ".path", ".runner", ".credentials", ".credentials_rsaparams"):
            (self.root / name).write_text("data")
        result = self.shell('DIR="$TEST_ROOT"; wipe_state')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            (self.root / "_toolcache/node/20.11.0/installed").read_text(), "cached tool")
        self.assertFalse((self.root / "_work").exists())
        for name in (".runner", ".credentials", ".credentials_rsaparams"):
            self.assertFalse((self.root / name).exists())
        for name in (".env", ".path"):
            self.assertTrue((self.root / name).exists())

    def test_consecutive_cycles_reuse_installed_tool_and_reset_checkout(self):
        # runsvc mock = one job: installs a tool on the first cycle, reuses it
        # on the second, and leaves a per-cycle checkout marker in _work.
        self.make_runner(runsvc_code=r'''
count=$(( $(cat "$TEST_ROOT/cycle" 2>/dev/null || echo 0) + 1 ))
echo "$count" > "$TEST_ROOT/cycle"
mkdir -p _work
touch "_work/job${count}"
tool="$AGENT_TOOLSDIRECTORY/node/20.11.0/installed"
if [[ -f "$tool" ]]; then
  echo "reused" >> "$TEST_ROOT/reuse-log"
else
  mkdir -p "${tool%/*}"
  touch "$tool"
fi
''')
        result = self.shell('''
DIR="$TEST_ROOT"; RUNNER_URL=https://github.com/acme
fetch_registration_token() { FETCHED_TOKEN=secret; }
if ! run_cycle || ! run_cycle; then exit 99; fi
printf '%s' "$AGENT_TOOLSDIRECTORY"
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, str(self.root / "_toolcache"))
        # Second job reused the installed tool instead of reinstalling it.
        self.assertEqual((self.root / "reuse-log").read_text().splitlines(), ["reused"])
        # Checkout/temp state still reset between jobs; registration re-created.
        self.assertFalse((self.root / "_work/job1").exists())
        self.assertTrue((self.root / "_work/job2").exists())
        self.assertTrue((self.root / ".runner").exists())

    def test_legacy_work_tool_migrates_between_jobs(self):
        (self.root / "_work/_tool/node/20.11.0").mkdir(parents=True)
        (self.root / "_work/_tool/node/20.11.0/installed").write_text("cached tool")
        result = self.shell('DIR="$TEST_ROOT"; wipe_state')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Migrated legacy _work/_tool cache", result.stderr)
        self.assertEqual(
            (self.root / "_toolcache/node/20.11.0/installed").read_text(), "cached tool")
        self.assertFalse((self.root / "_work").exists())

    def test_aliases_into_disposable_storage_fail_before_wipe(self):
        (self.root / "_work/_tool").mkdir(parents=True)
        marker = self.root / "_work/_tool/keep"
        marker.write_text("installed")
        (self.root / "alias").symlink_to(self.root / "_work/_tool")
        for value in (str(self.root / "alias"), str(self.root / "safe/../_work/_tool")):
            with self.subTest(value=value):
                result = self.shell('DIR="$TEST_ROOT"; wipe_state', AGENT_TOOLSDIRECTORY=value)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(marker.read_text(), "installed")

    def test_runner_toolsdirectory_precedes_agent_and_env_file_is_validated(self):
        result = self.shell('DIR="$TEST_ROOT"; resolve_tool_cache_dir; echo "$TOOL_CACHE_DIR"',
                            RUNNER_TOOLSDIRECTORY=str(self.root / "preferred"),
                            AGENT_TOOLSDIRECTORY=str(self.root / "ignored"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), str(self.root / "preferred"))
        (self.root / ".env").write_text(f"RUNNER_TOOLSDIRECTORY={self.root}/_work/_tool\n")
        result = self.shell('DIR="$TEST_ROOT"; wipe_state')
        self.assertNotEqual(result.returncode, 0)

    def test_unowned_cache_destination_is_rejected(self):
        result = self.shell('DIR="$TEST_ROOT"; resolve_tool_cache_dir', AGENT_TOOLSDIRECTORY="/usr/bin")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("owned by the invoking user", result.stderr)

    def test_existing_cache_is_never_merged(self):
        (self.root / "_work/_tool/node").mkdir(parents=True)
        (self.root / "_work/_tool/node/old").write_text("legacy")
        (self.root / "_toolcache/node").mkdir(parents=True)
        (self.root / "_toolcache/node/current").write_text("current")
        result = self.shell('DIR="$TEST_ROOT"; wipe_state')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("not merging", result.stderr)
        self.assertEqual((self.root / "_toolcache/node/current").read_text(), "current")
        self.assertFalse((self.root / "_toolcache/node/old").exists())
        self.assertFalse((self.root / "_work").exists())

    def test_symlinked_legacy_tool_dir_is_not_migrated(self):
        (self.root / "elsewhere").mkdir()
        (self.root / "_work").mkdir()
        (self.root / "_work/_tool").symlink_to(self.root / "elsewhere", target_is_directory=True)
        result = self.shell('DIR="$TEST_ROOT"; wipe_state')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("symlink", result.stderr)
        self.assertFalse((self.root / "_toolcache").exists())
        self.assertTrue((self.root / "elsewhere").is_dir())

    def test_operator_override_location_is_honored(self):
        shared = self.root / "shared-tools"
        self.make_runner(runsvc_code='echo "$AGENT_TOOLSDIRECTORY" > "$TEST_ROOT/seen-tooldir"')
        result = self.shell('''
DIR="$TEST_ROOT"; RUNNER_URL=https://github.com/acme
fetch_registration_token() { FETCHED_TOKEN=secret; }
if ! run_cycle; then exit 99; fi
printf '%s' "$TOOL_CACHE_DIR"
''', AGENT_TOOLSDIRECTORY=str(shared))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, str(shared))
        self.assertEqual((self.root / "seen-tooldir").read_text().strip(), str(shared))

    def test_relative_tool_cache_path_is_rejected_without_wiping(self):
        (self.root / "_work").mkdir()
        (self.root / "_work/keep").write_text("not wiped")
        result = self.shell('''
DIR="$TEST_ROOT"
if wipe_state; then exit 97; fi
''', AGENT_TOOLSDIRECTORY="relative/path")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("absolute path", result.stderr)
        self.assertEqual((self.root / "_work/keep").read_text(), "not wiped")

    def test_tool_cache_inside_work_is_rejected(self):
        result = self.shell('''
DIR="$TEST_ROOT"
if resolve_tool_cache_dir; then exit 97; fi
''', AGENT_TOOLSDIRECTORY=str(self.root / "_work/_tool"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("OUTSIDE", result.stderr)

    def test_per_runner_defaults_are_distinct(self):
        # Two runners default to disjoint caches, so concurrent jobs can never
        # corrupt each other's tool installs.
        result = self.shell('''
DIR="$TEST_ROOT/runner-1"; resolve_tool_cache_dir || exit 97; first="$TOOL_CACHE_DIR"
unset AGENT_TOOLSDIRECTORY   # each runner's supervisor is a separate process
DIR="$TEST_ROOT/runner-2"; resolve_tool_cache_dir || exit 98
[[ "$first" != "$TOOL_CACHE_DIR" ]] || exit 99
printf '%s\n%s' "$first" "$TOOL_CACHE_DIR"
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [
            str(self.root / "runner-1/_toolcache"),
            str(self.root / "runner-2/_toolcache"),
        ])

    def test_sandbox_profile_allows_external_tool_cache(self):
        (self.root / "runner-1").mkdir()
        external = self.root / "ext-tools"
        result = self.shell('''
DIR="$TEST_ROOT/runner-1"
sandbox-exec() { :; }
resolve_tool_cache_dir || exit 97
SANDBOX=1
prepare_sandbox_profile || exit 98
''', AGENT_TOOLSDIRECTORY=str(external))
        self.assertEqual(result.returncode, 0, result.stderr)
        profile = (self.root / "runner-1/.sandbox-exec.sb").read_text()
        self.assertIn(f'(allow file-write* (subpath "{external}"))', profile)

    def test_default_cache_inside_runner_tree_needs_no_extra_sandbox_rule(self):
        (self.root / "runner-1").mkdir()
        result = self.shell('''
DIR="$TEST_ROOT/runner-1"
sandbox-exec() { :; }
resolve_tool_cache_dir || exit 97
SANDBOX=1
prepare_sandbox_profile || exit 98
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        profile = (self.root / "runner-1/.sandbox-exec.sb").read_text()
        self.assertNotIn("_toolcache", profile)


class SetupToolCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.runner_root = self.root / "runners"
        self.plists = self.root / "plists"
        for path in (self.runner_root, self.plists, self.root / "worktmp"):
            path.mkdir(parents=True)

    def make_runner(self, index):
        directory = self.runner_root / f"runner-{index}"
        (directory / "bin").mkdir(parents=True)
        config = directory / "config.sh"
        config.write_text("#!/bin/bash\ntouch .runner\n")
        config.chmod(0o755)
        runsvc = directory / "bin/runsvc.sh"
        runsvc.write_text("#!/bin/bash\nexit 0\n")
        return directory

    HARNESS = r'''
trap - EXIT
RUNNER_ROOT="$TEST_ROOT/runners"; LAUNCH_DAEMON_DIR="$TEST_ROOT/plists"
WORK_TMPDIR="$TEST_ROOT/worktmp"
REAL_USER=fixture; REAL_GROUP=fixture; REAL_UID=501; REAL_HOME="$TEST_ROOT/home"
RUNNER_URL="https://github.com/acme"; SCOPE_SLUG="acme"
LABELS="macos,self-hosted"; NAME_PREFIX=""; EPHEMERAL=0; TOKEN=dummy
as_user() { "$@"; }
xattr() { :; }
plutil() { return 0; }
sudo() {
  case "$1" in
    launchctl)
      shift
      case "$1" in
        print) [[ -f "$TEST_ROOT/loaded-${2##*/}" ]] ;;
        bootstrap) local stem="${3##*/}"; touch "$TEST_ROOT/loaded-${stem%.plist}" ;;
        asuser) return 1 ;;   # no gui agent loaded in this fixture
        *) return 0 ;;
      esac ;;
    chown) return 0 ;;
    *) "$@" ;;
  esac
}
'''

    def shell(self, body, **env):
        return subprocess.run(
            ["/bin/bash", "-c",
             definitions("runner-setup") + '\n' + self.HARNESS + '\n' + body],
            env=dict(os.environ, TEST_ROOT=str(self.root), **env),
            text=True, capture_output=True)

    def test_plist_exports_distinct_per_runner_tool_caches(self):
        for index in (1, 2):
            self.make_runner(index)
        result = self.shell('''
if ! configure_runner 1 || ! configure_runner 2; then exit 97; fi
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        for index in (1, 2):
            plist = (self.plists / f"com.github.runner-{index}.plist").read_text()
            cache = self.runner_root / f"runner-{index}/_toolcache"
            self.assertIn("<key>AGENT_TOOLSDIRECTORY</key>", plist)
            self.assertIn(f"<string>{cache}</string>", plist)
            self.assertTrue(cache.is_dir())
        # Concurrent consumers: no two runners share a writable cache.
        self.assertNotIn(
            f"<string>{self.runner_root}/runner-1/_toolcache</string>",
            (self.plists / "com.github.runner-2.plist").read_text())

    def test_tool_cache_dir_override_lands_in_plist(self):
        self.make_runner(1)
        shared = self.root / "shared-tools"
        result = self.shell(f'''
TOOL_CACHE_DIR="{shared}"
if ! configure_runner 1; then exit 97; fi
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        plist = (self.plists / "com.github.runner-1.plist").read_text()
        self.assertIn(f"<string>{shared}</string>", plist)
        self.assertTrue(shared.is_dir())

    def test_relative_override_is_usage_error(self):
        result = self.shell(
            'parse_args --org acme --token dummy --tool-cache-dir rel/path')
        self.assertEqual(result.returncode, 2)
        self.assertIn("absolute path", result.stderr)

    def test_override_inside_work_is_usage_error(self):
        result = self.shell(
            'parse_args --org acme --token dummy '
            '--tool-cache-dir "$TEST_ROOT/runners/runner-1/_work/_tool"')
        self.assertEqual(result.returncode, 2)
        self.assertIn("outside", result.stderr)


class UpgradeToolCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.runner_root = self.root / "runners"
        self.runner_root.mkdir()
        (self.root / "plists").mkdir()
        (self.root / "plists/com.github.runner-1.plist").touch()
        with tarfile.open(self.root / "runner.tar.gz", "w:gz") as archive:
            for name, mode, contents in (
                ("bin/runsvc.sh", 0o644, "#!/bin/bash\nexit 0\n"),
                ("bin/Runner.Listener", 0o755, "#!/bin/bash\necho 2.337.0\n"),
            ):
                data = contents.encode()
                entry = tarfile.TarInfo(name)
                entry.mode = mode
                entry.size = len(data)
                archive.addfile(entry, io.BytesIO(data))

    def runner(self, name="runner-1", version="2.335.1", ready=False,
               workspace=True, toolcache=False):
        directory = self.runner_root / name
        (directory / "bin").mkdir(parents=True)
        (directory / "_diag").mkdir()
        if workspace:
            (directory / "_work/project").mkdir(parents=True)
            (directory / "_work/project/artifact").write_text(f"{name} workspace")
        if toolcache:
            (directory / "_toolcache/node/20.11.0").mkdir(parents=True)
            (directory / "_toolcache/node/20.11.0/installed").write_text(f"{name} tool cache")
        for path, mode in ((directory / "bin/runsvc.sh", 0o755),
                           (directory / "bin/Runner.Listener", 0o755)):
            path.write_text(f"#!/bin/bash\necho {version}\n" if "Listener" in path.name
                            else "#!/bin/bash\nexit 0\n")
            path.chmod(mode)
        (directory / ".runner").write_text("fixture .runner")
        (directory / ".credentials").write_text("fixture .credentials")
        if ready:
            (directory / "_diag/Runner_latest.log").write_text("old process: Listening for Jobs\n")
        return directory

    HARNESS = r'''
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
    bootout) rm -f "$TEST_ROOT/loaded" ;;
    print) [[ -f "$TEST_ROOT/loaded" ]] ;;
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
      printf 'new process %s: Listening for Jobs\n' "$version" >> "$dir/_diag/Runner_latest.log"
      ;;
    enable) return 0 ;;
    kickstart) echo 'Unexpected redundant service restart' >&2; return 94 ;;
    *) echo "Unexpected launchctl: $*" >&2; return 96 ;;
  esac
}
'''

    def shell(self, body, **env):
        return subprocess.run(
            ["/bin/bash", "-c",
             definitions("runner-upgrade") + '\n' + self.HARNESS + '\n' + body],
            env=dict(os.environ, TEST_ROOT=str(self.root), **env),
            text=True, capture_output=True, timeout=20)

    def test_upgrade_preserves_persistent_tool_cache(self):
        directory = self.runner(ready=True, toolcache=True)
        result = self.shell('if ! upgrade_runner runner-1; then exit 42; fi')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            (directory / "_toolcache/node/20.11.0/installed").read_text(),
            "runner-1 tool cache")
        self.assertFalse((self.runner_root / "runner-1.prev/_toolcache").exists())
        self.assertEqual(
            (directory / "_work/project/artifact").read_text(), "runner-1 workspace")

    def test_rollback_returns_tool_cache_to_previous_install(self):
        self.runner(version="2.337.0", toolcache=True)
        self.runner("runner-1.prev", ready=True, workspace=False)
        result = self.shell('if ! rollback_runner runner-1; then exit 42; fi')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("rolled back and listening", result.stderr)
        self.assertEqual(
            (self.runner_root / "runner-1/_toolcache/node/20.11.0/installed").read_text(),
            "runner-1 tool cache")
        self.assertFalse((self.runner_root / "runner-1.prev").exists())

    def test_conflicting_tool_caches_are_preserved_and_rollback_fails(self):
        current = self.runner(version="2.337.0", toolcache=True)
        previous = self.runner("runner-1.prev", workspace=False, toolcache=True)
        result = self.shell('if rollback_runner runner-1; then exit 42; fi')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            (current / "_toolcache/node/20.11.0/installed").read_text(),
            "runner-1 tool cache")
        self.assertEqual(
            (previous / "_toolcache/node/20.11.0/installed").read_text(),
            "runner-1.prev tool cache")
        self.assertNotIn("bootstrap", (self.root / "launchctl-commands").read_text())

    def test_repair_plan_keeps_plist_tool_cache_environment(self):
        # repair rewrites only ProgramArguments; the plist's EnvironmentVariables
        # (including AGENT_TOOLSDIRECTORY) must survive re-registration.
        runner = self.runner_root / "runner-1"
        cache = runner / "_toolcache"
        plist = self.root / "plists/com.github.runner-1.plist"
        plist.write_bytes(plistlib.dumps({
            "Label": "com.github.runner-1", "WorkingDirectory": str(runner),
            "ProgramArguments": ["/opt/homebrew/bin/runner-ephemeral",
                                 "--dir", str(runner), "--org", "acme",
                                 "--labels", "macos"],
            "EnvironmentVariables": {"HOME": "/Users/fixture",
                                     "AGENT_TOOLSDIRECTORY": str(cache)},
        }))
        result = self.shell(f'''
REPAIR_LABELS="macos,self-hosted"; REPAIR_LABELS_SET=1
repair_plist_plan "{plist}" "{runner}" "https://github.com/acme"
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        plan = json.loads(result.stdout)
        self.assertTrue(plan["supervisor"])
        self.assertNotIn("--org", plan["arguments"])
        self.assertIn("--url", plan["arguments"])
        # Apply the plan exactly as cmd_repair does (plutil -replace
        # ProgramArguments) and confirm the cache environment survives.
        updated = self.root / "updated.plist"
        updated.write_bytes(plist.read_bytes())
        subprocess.run(
            ["plutil", "-replace", "ProgramArguments", "-json",
             json.dumps(plan["arguments"]), str(updated)],
            check=True, capture_output=True)
        reloaded = plistlib.loads(updated.read_bytes())
        self.assertEqual(reloaded["EnvironmentVariables"]["AGENT_TOOLSDIRECTORY"], str(cache))
        self.assertEqual(reloaded["ProgramArguments"], plan["arguments"])


class PruneToolCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.fleet = self.root / "fleet"
        self.home = self.root / "home"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for index in (1, 2):
            for directory in ("repo", "_tool", "_temp"):
                self.touch(self.fleet / f"runner-{index}/_work" / directory / "keep")
            self.touch(self.fleet / f"runner-{index}/_toolcache/node/keep")
        self.touch(self.home / ".npm/keep")
        for name, body in {
            "uname": "echo Darwin",
            "whoami": "echo runner-test",
            "dscl": f'echo "NFSHomeDirectory: {self.home}"',
            "df": 'echo "Filesystem 1024-blocks Used Available Capacity Mounted"; echo "disk 999999999 1 ${TEST_FREE_KB:-999999999} 1% /"',
            "sudo": "exit 1",
        }.items():
            path = self.bin / name
            path.write_text("#!/bin/bash\n" + body + "\n")
            path.chmod(0o755)
        self.env = dict(os.environ, PATH=f"{self.bin}:/usr/bin:/bin", RUNNER_PRUNE_DOCKER="0")
        for key in ("RUNNER_TEMP", "GITHUB_ACTIONS", "SUDO_USER"):
            self.env.pop(key, None)

    def touch(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("valuable cached or job data")

    def run_prune(self, *args, **env):
        return subprocess.run(
            ["/bin/bash", str(REPO / "runner-prune"), "--runners-dir", str(self.fleet), *args],
            env=dict(self.env, **env), capture_output=True, text=True)

    def test_hook_prune_keeps_persistent_tool_cache(self):
        result = self.run_prune(
            RUNNER_TEMP=str(self.fleet / "runner-1/_work/_temp"), GITHUB_ACTIONS="true")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.fleet / "runner-1/_work/repo").exists())
        self.assertTrue((self.fleet / "runner-1/_toolcache/node/keep").exists())
        self.assertTrue((self.fleet / "runner-2/_toolcache/node/keep").exists())

    def test_offline_prune_without_purge_keeps_tool_cache(self):
        result = self.run_prune("--all", "--offline")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.fleet / "runner-1/_work/repo").exists())
        self.assertTrue((self.fleet / "runner-1/_toolcache/node/keep").exists())
        self.assertTrue((self.home / ".npm/keep").exists())

    def test_offline_purge_evicts_tool_caches(self):
        result = self.run_prune("--all", "--offline", "--purge-caches")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.fleet / "runner-1/_toolcache").exists())
        self.assertFalse((self.fleet / "runner-2/_toolcache").exists())
        self.assertFalse((self.home / ".npm").exists())
        # Legacy in-work caches are still preserved for non-relocated runners.
        self.assertTrue((self.fleet / "runner-1/_work/_tool/keep").exists())

    def test_offline_purge_dry_run_keeps_tool_caches(self):
        result = self.run_prune("--all", "--offline", "--purge-caches", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.fleet / "runner-1/_toolcache/node/keep").exists())


if __name__ == "__main__":
    unittest.main()
