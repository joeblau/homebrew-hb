"""RAM-backed build scratch tests (issue #56).

Covers the runner-ramscratch helper (mount lifecycle, path validation,
host-wide budget, memory headroom, stale state after reboot, unmount
refusal while consumers hold the mount, SSD fallback signaling) and its
lifecycle wiring in runner-ephemeral / runner-setup / runner-cleanup.
hdiutil/newfs_hfs/df/vm_stat/sysctl are mocked shell functions or PATH
fixtures; no real sudo, hdiutil, launchd, network, or /opt access.
"""
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


AMPLE_VM_STAT = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                               1000000.
Pages active:                              500000.
Pages inactive:                           1000000.
Pages wired down:                          100000.
"""

LOW_VM_STAT = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                                   100.
Pages active:                              500000.
Pages inactive:                               100.
Pages wired down:                          600000.
"""

# hdiutil attach -nomount prints the RAM device; the mount form "mounts" by
# switching the mocked df device. detach records itself and unmounts again
# (or refuses when the detach-fails fixture exists, simulating a consumer
# holding files open on the mount).
MOCKS = r'''
hdiutil() {
  case "$1" in
    attach)
      shift
      local nomount=0
      while [[ $# -gt 0 ]]; do
        case "$1" in
          -nomount) nomount=1 ;;
          -mountpoint) shift ;;
        esac
        shift
      done
      if [[ "${nomount}" -eq 1 ]]; then
        printf '/dev/disk9\n'
      else
        printf '/dev/disk9\n' > "$TEST_ROOT/df-device"
      fi ;;
    detach)
      if [[ -f "$TEST_ROOT/detach-fails" ]]; then
        echo "hdiutil: couldn't unmount disk - Resource busy" >&2
        return 1
      fi
      printf 'detach %s\n' "$2" >> "$TEST_ROOT/detach-log"
      rm -f "$TEST_ROOT/df-device" ;;
    *) echo "unexpected hdiutil: $*" >&2; return 96 ;;
  esac
}
newfs_hfs() { :; }
df() {
  local dev; dev="$(cat "$TEST_ROOT/df-device" 2>/dev/null || echo /dev/disk1)"
  printf 'Filesystem 512-blocks Used Available Capacity Mounted on\n'
  printf '%s 4096000 1000 4095000 1%% %s\n' "${dev}" "$2"
}
sysctl() { printf '%s\n' "${TEST_MEMSIZE:-34359738368}"; }
vm_stat() { cat "$TEST_ROOT/vm_stat"; }
'''


class RamScratchHelperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.state = self.root / "state"
        (self.root / "vm_stat").write_text(AMPLE_VM_STAT)

    def write_config(self, budget=2048, min_free=4096):
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "config").write_text(
            f"TOTAL_BUDGET_MB={budget}\nMIN_FREE_MEMORY_MB={min_free}\n")

    def shell(self, body, **env):
        return subprocess.run(
            ["/bin/bash", "-c",
             definitions("runner-ramscratch") + "\n" + MOCKS + "\n" + body],
            env=dict(os.environ, TEST_ROOT=str(self.root),
                     RUNNER_RAMSCRATCH_DIR=str(self.state), **env),
            text=True, capture_output=True)

    def run_cli(self, *args):
        return subprocess.run(["/bin/bash", str(REPO / "runner-ramscratch"), *args],
                              text=True, capture_output=True)

    # --- usage ------------------------------------------------------------
    def test_help_and_usage_errors(self):
        result = self.run_cli("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("USAGE:", result.stderr)
        self.assertEqual(self.run_cli().returncode, 2)
        self.assertEqual(self.run_cli("bogus").returncode, 2)
        missing_size = self.run_cli("alloc", "--runner", "r1", "--job", "j1")
        self.assertEqual(missing_size.returncode, 2)
        self.assertIn("size-mb", missing_size.stderr)

    def test_config_command_requires_explicit_budget(self):
        result = self.run_cli("config")
        self.assertEqual(result.returncode, 2)
        self.assertIn("--total-budget-mb", result.stderr)

    # --- allocation ---------------------------------------------------------
    def test_alloc_requires_configuration(self):
        result = self.shell("cmd_alloc --runner runner-1 --job cycle-1 --size-mb 512")
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn("not configured", result.stderr)
        self.assertFalse((self.state / "volume").exists())

    def test_alloc_creates_bounded_volume_and_per_job_dirs(self):
        self.write_config()
        result = self.shell('''
path1="$(cmd_alloc --runner runner-1 --job cycle-1 --size-mb 512)" || exit 97
path2="$(cmd_alloc --runner runner-2 --job cycle-1 --size-mb 512)" || exit 98
[[ "$path1" != "$path2" ]] || exit 99
printf '%s\n%s' "$path1" "$path2"
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        path1, path2 = result.stdout.splitlines()
        self.assertEqual(path1, str(self.state / "volume/runner-1/cycle-1"))
        self.assertEqual(path2, str(self.state / "volume/runner-2/cycle-1"))
        self.assertTrue(Path(path1).is_dir())
        self.assertTrue(Path(path2).is_dir())
        # One bounded volume: recorded device sized at the total budget.
        state = (self.state / "state").read_text()
        self.assertIn("DEVICE=/dev/disk9", state)
        self.assertIn("SIZE_MB=2048", state)

    def test_budget_is_enforced_across_runners(self):
        self.write_config(budget=1024)
        result = self.shell('''
cmd_alloc --runner runner-1 --job cycle-1 --size-mb 768 >/dev/null || exit 97
cmd_alloc --runner runner-2 --job cycle-1 --size-mb 512 >/dev/null
''')
        self.assertEqual(result.returncode, 4, result.stderr)
        self.assertIn("budget exhausted", result.stderr)
        # The refused allocation created nothing; the granted one survives.
        self.assertTrue((self.state / "volume/runner-1/cycle-1").is_dir())
        self.assertFalse((self.state / "volume/runner-2").exists())
        self.assertFalse((self.state / "allocs/runner-2__cycle-1").exists())

    def test_low_memory_refuses_allocation(self):
        self.write_config()
        (self.root / "vm_stat").write_text(LOW_VM_STAT)
        result = self.shell("cmd_alloc --runner runner-1 --job cycle-1 --size-mb 512")
        self.assertEqual(result.returncode, 4, result.stderr)
        self.assertIn("headroom", result.stderr)
        self.assertFalse((self.state / "state").exists())

    def test_budget_must_be_smaller_than_physical_ram(self):
        self.write_config(budget=99999999)
        result = self.shell("cmd_alloc --runner runner-1 --job cycle-1 --size-mb 512")
        self.assertEqual(result.returncode, 4, result.stderr)
        self.assertIn("physical RAM", result.stderr)

    def test_volume_owned_by_another_user_is_detached_and_rejected(self):
        self.write_config()
        result = self.shell('''
stat() { echo root; }
cmd_alloc --runner runner-1 --job cycle-1 --size-mb 512
''')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("not owned by the invoking user", result.stderr)
        self.assertIn("detach /dev/disk9",
                      (self.root / "detach-log").read_text())
        self.assertFalse((self.state / "state").exists())

    # --- clean / unmount lifecycle ------------------------------------------
    def alloc_two(self):
        result = self.shell('''
cmd_alloc --runner runner-1 --job cycle-1 --size-mb 512 >/dev/null || exit 97
cmd_alloc --runner runner-2 --job cycle-1 --size-mb 512 >/dev/null || exit 98
''')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_clean_removes_only_own_job_and_detaches_when_empty(self):
        self.write_config()
        self.alloc_two()
        (self.state / "volume/runner-1/cycle-1/output.o").write_text("job 1")
        (self.state / "volume/runner-2/cycle-1/output.o").write_text("job 2")
        result = self.shell(
            "cmd_clean --runner runner-1 --job cycle-1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.state / "volume/runner-1").exists())
        # Another job's workspace is untouched and the shared volume stays up.
        self.assertEqual(
            (self.state / "volume/runner-2/cycle-1/output.o").read_text(), "job 2")
        self.assertFalse((self.root / "detach-log").exists())
        self.assertIn("stays mounted", result.stderr)
        # Last consumer out: the volume is detached.
        result = self.shell("cmd_clean --runner runner-2 --job cycle-1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("detach /dev/disk9",
                      (self.root / "detach-log").read_text())
        self.assertFalse((self.state / "state").exists())

    def test_clean_rejects_traversal_and_symlinked_targets(self):
        self.write_config()
        self.alloc_two()
        secret = self.root / "secret"
        secret.mkdir()
        (secret / "keep").write_text("do not delete")
        os.symlink(secret, self.state / "volume/runner-1/evil")
        result = self.shell('''
if cmd_clean --runner runner-1 --job evil; then exit 97; fi
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("symlink", result.stderr)
        self.assertEqual((secret / "keep").read_text(), "do not delete")
        self.assertTrue((self.state / "volume/runner-2/cycle-1").is_dir())
        # Name validation rejects traversal outright (usage error, exit 2).
        result = self.shell("cmd_clean --runner runner-1 --job ../runner-2")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertTrue((self.state / "volume/runner-2/cycle-1").is_dir())

    def test_clean_all_requires_offline_and_then_unmounts(self):
        self.write_config()
        self.alloc_two()
        refused = self.run_cli("clean", "--all")
        self.assertEqual(refused.returncode, 2)
        self.assertIn("--offline", refused.stderr)
        result = self.shell("cmd_clean --all --offline")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.state / "volume/runner-1").exists())
        self.assertFalse((self.state / "volume/runner-2").exists())
        self.assertIn("detach /dev/disk9",
                      (self.root / "detach-log").read_text())

    def test_unmount_refused_while_consumer_holds_mount(self):
        self.write_config()
        (self.root / "detach-fails").touch()
        result = self.shell('''
cmd_alloc --runner runner-1 --job cycle-1 --size-mb 512 >/dev/null || exit 97
cmd_clean --runner runner-1 --job cycle-1 || exit 98
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("still hold the mount", result.stderr)
        # State retained so a later clean retries the detach.
        self.assertTrue((self.state / "state").exists())
        self.assertFalse((self.root / "detach-log").exists())

    # --- stale state after reboot -------------------------------------------
    def test_stale_state_after_reboot_is_reset(self):
        self.write_config()
        self.alloc_two()
        # Simulate a reboot: the RAM volume is gone (df no longer reports the
        # recorded device) but the on-SSD ledger survived.
        (self.root / "df-device").write_text("/dev/disk1\n")
        result = self.shell('''
cmd_alloc --runner runner-1 --job cycle-2 --size-mb 512 >/dev/null || exit 97
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Stale RAM scratch state", result.stderr)
        # Pre-reboot allocations were dropped from the ledger, not charged
        # against the budget forever.
        self.assertFalse((self.state / "allocs/runner-2__cycle-1").exists())
        self.assertTrue((self.state / "allocs/runner-1__cycle-2").exists())

    # --- inspect --------------------------------------------------------------
    def test_inspect_reports_config_volume_and_allocations(self):
        self.write_config()
        result = self.shell("cmd_inspect")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("budget 2048 MB", result.stderr)
        self.assertIn("not mounted", result.stderr)
        result = self.shell('''
cmd_alloc --runner runner-1 --job cycle-1 --size-mb 512 >/dev/null || exit 97
cmd_inspect
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("/dev/disk9", result.stderr)
        self.assertIn("runner-1/cycle-1: 512 MB", result.stderr)
        self.assertIn("512 MB of 2048 MB", result.stderr)

    def test_inspect_without_config_reports_feature_off(self):
        result = self.shell("cmd_inspect")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("feature is OFF", result.stderr)


class EphemeralRamScratchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.mock_bin = self.root / "mockbin"
        self.mock_bin.mkdir()
        helper = self.mock_bin / "runner-ramscratch"
        helper.write_text("""#!/bin/bash
echo "ramscratch $*" >> "$TEST_ROOT/ramscratch-calls"
if [[ "$1" == "alloc" ]]; then
  [[ -z "${RAMSCRATCH_ALLOC_FAILS:-}" ]] || exit 4
  runner=""; job=""
  shift
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --runner) runner="$2"; shift 2 ;;
      --job) job="$2"; shift 2 ;;
      *) shift ;;
    esac
  done
  mkdir -p "$TEST_ROOT/ramvol/$runner/$job"
  printf '%s/%s/%s\\n' "$TEST_ROOT/ramvol" "$runner" "$job"
fi
""")
        helper.chmod(0o755)

    def shell(self, body, **env):
        return subprocess.run(
            ["/bin/bash", "-c", definitions("runner-ephemeral") + "\n" + body],
            env=dict(os.environ, TEST_ROOT=str(self.root),
                     RUNNER_RAMSCRATCH_BIN=str(self.mock_bin / "runner-ramscratch"),
                     PATH=f"{self.mock_bin}:{os.environ['PATH']}", **env),
            text=True, capture_output=True)

    def make_runner(self, runsvc_code="exit 0"):
        (self.root / "bin").mkdir(exist_ok=True)
        for name, code in (("config.sh", "touch .runner"), ("bin/runsvc.sh", runsvc_code)):
            path = self.root / name
            path.write_text("#!/bin/bash\n" + code + "\n")
            path.chmod(0o755)

    def calls(self):
        log = self.root / "ramscratch-calls"
        return log.read_text().splitlines() if log.exists() else []

    def test_cycle_allocates_then_releases_and_copies_out_artifacts(self):
        self.make_runner(
            'printf \'%s\\n\' "$RUNNER_RAM_SCRATCH" > "$TEST_ROOT/seen-scratch"\n'
            'mkdir -p "$RUNNER_RAM_SCRATCH/_out"\n'
            'echo artifact-bytes > "$RUNNER_RAM_SCRATCH/_out/result.txt"')
        result = self.shell('''
DIR="$TEST_ROOT"; RUNNER_URL=https://github.com/acme
RAM_SCRATCH=1; RAM_SCRATCH_MB=512
fetch_registration_token() { FETCHED_TOKEN=secret; }
if ! run_cycle 7; then exit 99; fi
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        runner_dir = self.root.name.replace("/", "-")
        scratch = self.root / f"ramvol/{runner_dir}/cycle-7"
        self.assertEqual((self.root / "seen-scratch").read_text().strip(), str(scratch))
        # alloc before the job, clean after; artifacts out BEFORE teardown.
        self.assertEqual(self.calls(), [
            f"ramscratch alloc --runner {runner_dir} --job cycle-7 --size-mb 512",
            f"ramscratch clean --runner {runner_dir} --job cycle-7",
        ])
        copies = list((self.root / "_diag/ramscratch").glob("cycle-7-*/result.txt"))
        self.assertEqual(len(copies), 1)
        self.assertEqual(copies[0].read_text().strip(), "artifact-bytes")
        # The .env pointer is cleared at teardown, not left dangling.
        env_file = self.root / ".env"
        if env_file.exists():
            self.assertNotIn("RUNNER_RAM_SCRATCH", env_file.read_text())

    def test_env_file_preserves_existing_hooks(self):
        self.make_runner()
        (self.root / ".env").write_text(
            "ACTIONS_RUNNER_HOOK_JOB_COMPLETED=/opt/hooks/done.sh\nOTHER=1\n")
        result = self.shell('''
DIR="$TEST_ROOT"; RUNNER_URL=https://github.com/acme
RAM_SCRATCH=1; RAM_SCRATCH_MB=512
fetch_registration_token() { FETCHED_TOKEN=secret; }
if ! run_cycle 1; then exit 99; fi
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        env_text = (self.root / ".env").read_text()
        self.assertIn("ACTIONS_RUNNER_HOOK_JOB_COMPLETED=/opt/hooks/done.sh", env_text)
        self.assertIn("OTHER=1", env_text)

    def test_allocation_failure_falls_back_to_ssd(self):
        self.make_runner('printf \'%s\\n\' "$RUNNER_RAM_SCRATCH" > "$TEST_ROOT/seen-scratch"')
        result = self.shell('''
DIR="$TEST_ROOT"; RUNNER_URL=https://github.com/acme
RAM_SCRATCH=1; RAM_SCRATCH_MB=512
fetch_registration_token() { FETCHED_TOKEN=secret; }
if ! run_cycle 1; then exit 99; fi
''', RAMSCRATCH_ALLOC_FAILS="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("falling back to SSD", result.stderr)
        self.assertEqual((self.root / "seen-scratch").read_text().strip(),
                         str(self.root / "_work/_scratch"))
        # No clean call: the SSD fallback resets with the next cycle's wipe.
        self.assertEqual(len(self.calls()), 1)
        self.assertIn("alloc", self.calls()[0])

    def test_disabled_by_default_makes_no_helper_calls(self):
        self.make_runner('echo "${RUNNER_RAM_SCRATCH:-unset}" > "$TEST_ROOT/seen-scratch"')
        (self.root / ".env").write_text("KEEP=me\n")
        result = self.shell('''
DIR="$TEST_ROOT"; RUNNER_URL=https://github.com/acme
fetch_registration_token() { FETCHED_TOKEN=secret; }
if ! run_cycle 1; then exit 99; fi
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), [])
        self.assertEqual((self.root / "seen-scratch").read_text().strip(), "unset")
        self.assertEqual((self.root / ".env").read_text(), "KEEP=me\n")

    def test_shutdown_copies_out_and_releases_interrupted_job(self):
        scratch = self.root / "ramvol/runner-1/cycle-3"
        (scratch / "_out").mkdir(parents=True)
        (scratch / "_out/partial.log").write_text("interrupted job log")
        (self.root / "_diag").mkdir()
        result = self.shell('''
DIR="$TEST_ROOT"
RAM_SCRATCH=1; RAM_SCRATCH_MB=512
RAM_SCRATCH_PATH="$TEST_ROOT/ramvol/runner-1/cycle-3"; RAM_SCRATCH_IS_RAM=1
RAM_SCRATCH_RUNNER="runner-1"; RAM_SCRATCH_JOB="cycle-3"
CHILD_PID=""
shutdown
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"ramscratch clean --runner runner-1 --job cycle-3", self.calls())
        copies = list((self.root / "_diag/ramscratch").glob("cycle-3-*/partial.log"))
        self.assertEqual(len(copies), 1)
        self.assertEqual(copies[0].read_text().strip(), "interrupted job log")

    def test_sandbox_profile_includes_ram_scratch_path(self):
        (self.root / "runner-1").mkdir()
        result = self.shell('''
DIR="$TEST_ROOT/runner-1"
SANDBOX=1
RAM_SCRATCH_PATH="$TEST_ROOT/ramvol/runner-1/cycle-1"
sandbox-exec() { :; }
prepare_sandbox_profile || exit 97
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        profile = (self.root / "runner-1/.sandbox-exec.sb").read_text()
        self.assertIn(
            f'(allow file-write* (subpath "{self.root}/ramvol/runner-1/cycle-1"))',
            profile)

    def test_ram_scratch_requires_explicit_sizing(self):
        result = subprocess.run(
            ["/bin/bash", str(REPO / "runner-ephemeral"),
             "--dir", "/tmp/x", "--org", "acme", "--ram-scratch"],
            text=True, capture_output=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("--ram-scratch-mb", result.stderr)

    def test_env_var_enables_ram_scratch(self):
        result = subprocess.run(
            ["/bin/bash", str(REPO / "runner-ephemeral"),
             "--dir", "/tmp/x", "--org", "acme"],
            env=dict(os.environ, RUNNER_RAMSCRATCH="1"),
            text=True, capture_output=True)
        # Enabled via the plist env var; missing sizing is the same usage error.
        self.assertEqual(result.returncode, 2)
        self.assertIn("--ram-scratch-mb", result.stderr)


class SetupRamScratchTests(unittest.TestCase):
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
        asuser) return 1 ;;
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
             definitions("runner-setup") + "\n" + self.HARNESS + "\n" + body],
            env=dict(os.environ, TEST_ROOT=str(self.root), **env),
            text=True, capture_output=True)

    def test_ram_scratch_requires_ephemeral(self):
        result = self.shell(
            "parse_args --org acme --token dummy --ram-scratch "
            "--ram-scratch-budget-mb 2048 --ram-scratch-job-mb 512")
        self.assertEqual(result.returncode, 2)
        self.assertIn("requires --ephemeral", result.stderr)

    def test_ram_scratch_requires_explicit_sizing(self):
        result = self.shell("parse_args --org acme --token dummy --ephemeral --ram-scratch")
        self.assertEqual(result.returncode, 2)
        self.assertIn("--ram-scratch-budget-mb", result.stderr)
        result = self.shell(
            "parse_args --org acme --token dummy --ephemeral --ram-scratch "
            "--ram-scratch-budget-mb 2048")
        self.assertEqual(result.returncode, 2)
        self.assertIn("--ram-scratch-job-mb", result.stderr)

    def test_job_share_cannot_exceed_budget(self):
        result = self.shell(
            "parse_args --org acme --token dummy --ephemeral --ram-scratch "
            "--ram-scratch-budget-mb 512 --ram-scratch-job-mb 1024")
        self.assertEqual(result.returncode, 2)
        self.assertIn("exceeds", result.stderr)

    def test_config_written_with_runner_user_ownership(self):
        result = self.shell('''
RAM_SCRATCH=1; RAM_SCRATCH_BUDGET_MB=4096; RAM_SCRATCH_MIN_FREE_MB=2048
write_ramscratch_config || exit 97
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        config = (self.runner_root / "_ramscratch/config").read_text()
        self.assertIn("TOTAL_BUDGET_MB=4096", config)
        self.assertIn("MIN_FREE_MEMORY_MB=2048", config)

    def test_ephemeral_plist_carries_ram_scratch_args_and_env(self):
        self.make_runner(1)
        result = self.shell('''
EPHEMERAL=1; EPHEMERAL_BIN="$TEST_ROOT/runner-ephemeral"
RAM_SCRATCH=1; RAM_SCRATCH_JOB_MB=512; RAM_SCRATCH_BUDGET_MB=2048
touch "$EPHEMERAL_BIN"; chmod +x "$EPHEMERAL_BIN"
if ! configure_runner 1; then exit 97; fi
''', GITHUB_PAT="dummy")
        self.assertEqual(result.returncode, 0, result.stderr)
        plist = (self.plists / "com.github.runner-1.plist").read_text()
        self.assertIn("<string>--ram-scratch</string>", plist)
        self.assertIn("<string>--ram-scratch-mb</string>", plist)
        self.assertIn("<string>512</string>", plist)
        self.assertIn("<key>RUNNER_RAMSCRATCH</key>", plist)

    def test_plist_without_ram_scratch_stays_unchanged(self):
        self.make_runner(1)
        result = self.shell("if ! configure_runner 1; then exit 97; fi")
        self.assertEqual(result.returncode, 0, result.stderr)
        plist = (self.plists / "com.github.runner-1.plist").read_text()
        self.assertNotIn("RAMSCRATCH", plist)


class CleanupRamScratchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.runner_root = self.root / "runners"
        self.state = self.runner_root / "_ramscratch"
        self.state.mkdir(parents=True)
        self.mock_bin = self.root / "mockbin"
        self.mock_bin.mkdir()
        helper = self.mock_bin / "runner-ramscratch"
        helper.write_text(
            "#!/bin/bash\necho \"ramscratch $*\" >> \"$TEST_ROOT/ramscratch-calls\"\n")
        helper.chmod(0o755)

    def shell(self, body, **env):
        return subprocess.run(
            ["/bin/bash", "-c",
             definitions("runner-cleanup") + "\n" + self.HARNESS + "\n" + body],
            env=dict(os.environ, TEST_ROOT=str(self.root), **env),
            text=True, capture_output=True)

    HARNESS = r'''
trap - EXIT
RUNNER_ROOT="$TEST_ROOT/runners"
REAL_USER=fixture; REAL_GROUP=fixture; REAL_UID=501; REAL_HOME="$TEST_ROOT/home"
as_user() { "$@"; }
sudo() { "$@"; }
'''

    def test_teardown_unmounts_and_removes_state_dir(self):
        result = self.shell("teardown_ramscratch || exit 97",
                            RUNNER_RAMSCRATCH_BIN=str(self.mock_bin / "runner-ramscratch"))
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = (self.root / "ramscratch-calls").read_text().splitlines()
        self.assertEqual(calls, ["ramscratch clean --all --offline"])
        self.assertFalse(self.state.exists())

    def test_teardown_without_helper_leaves_state(self):
        result = self.shell("teardown_ramscratch || exit 97",
                            PATH="/usr/bin:/bin")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("not found", result.stderr)
        self.assertTrue(self.state.exists())

    def test_teardown_without_state_dir_is_noop(self):
        self.state.rmdir()
        result = self.shell('''
teardown_ramscratch || exit 97
runner-ramscratch() { echo unexpected >&2; exit 96; }
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root / "ramscratch-calls").exists())


if __name__ == "__main__":
    unittest.main()
