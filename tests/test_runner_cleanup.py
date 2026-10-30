"""runner-cleanup: full teardown, and --org/--keep-files deregistration that
keeps the install."""

import json
import os
from pathlib import Path
import plistlib
import shlex
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "bin" / "runner-cleanup"


class RunnerCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="runner-cleanup-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.runner_root = self.root / "runners"
        self.plist_dir = self.root / "plists"
        self.home = self.root / "home"
        self.stub_bin = self.root / "bin"
        self.runner_root.mkdir()
        self.plist_dir.mkdir()
        self.home.mkdir()
        self.stub_bin.mkdir()

    def runner(self, name="runner-1", org="acme", remove_rc=0):
        directory = self.runner_root / name
        (directory / "bin").mkdir(parents=True)
        (directory / "_toolcache").mkdir()
        (directory / "_work").mkdir()
        (directory / "_toolcache" / "keep").write_text("tool")
        (directory / "_work" / "keep").write_text("checkout")
        (directory / ".github_pat").write_text("pat-secret\n")
        (directory / ".credentials").write_text('{"scheme":"OAuth"}\n')
        (directory / ".runner").write_text(json.dumps({
            "agentId": 7,
            "agentName": f"mini-{name}",
            "gitHubUrl": f"https://github.com/{org}",
            "workFolder": "_work",
        }))
        config = directory / "config.sh"
        remove_runner = "rm -f .runner\n" if remove_rc == 0 else ""
        config.write_text(
            "#!/bin/bash\n"
            "printf '%s\\n' \"$3\" > \"$TEST_ROOT/token-seen\"\n"
            "printf '%s\\n' \"$1\" >> \"$TEST_ROOT/config-args\"\n"
            f"{remove_runner}exit {remove_rc}\n"
        )
        config.chmod(0o755)
        self.plist(name, org)
        return directory

    def plist(self, name, org):
        path = self.plist_dir / f"com.github.{name}.plist"
        with path.open("wb") as handle:
            plistlib.dump({
                "Label": f"com.github.{name}",
                "ProgramArguments": [
                    "/opt/homebrew/bin/runner-ephemeral",
                    "--dir", f"/opt/github-runners/{name}",
                    "--org", org,
                    "--labels", "macos",
                ],
            }, handle)
        return path

    def token_stub(self):
        stub = self.stub_bin / "runner-token"
        stub.write_text(
            "#!/bin/bash\n"
            "printf '%s\\n' \"$*\" > \"$TEST_ROOT/token-cmd\"\n"
            "if [[ \"$1\" == removal && \"$2\" == --org && \"$3\" == acme ]]; then\n"
            "  printf '%s\\n' 'minted-removal-token'\n"
            "  exit 0\n"
            "fi\n"
            "exit 1\n"
        )
        stub.chmod(0o755)

    def invoke(self, *args, **env):
        definitions = SCRIPT.read_text().rsplit('main "$@"', 1)[0]
        harness = r'''
trap - EXIT
RUNNER_ROOT="$TEST_ROOT/runners"
LAUNCH_DAEMON_DIR="$TEST_ROOT/plists"
BOOTOUT_RETRIES=1
detect_platform() { :; }
require_cmds() { :; }
detect_user() {
  REAL_USER=fixture
  REAL_GROUP=fixture
  REAL_UID=501
  REAL_HOME="$TEST_ROOT/home"
}
ensure_sudo() { :; }
as_user() { "$@"; }
sleep() { :; }
ps() {
  [[ -n "${FAKE_PS:-}" ]] && printf '%s\n' "${FAKE_PS}"
  return 0
}
sudo() {
  {
    printf 'sudo'
    local arg
    for arg in "$@"; do printf ' %s' "$arg"; done
    printf '\n'
  } >> "$TEST_ROOT/events"
  if [[ "$1" == launchctl && "$2" == print ]]; then
    [[ "${STUCK_DAEMON:-}" == 1 ]] && return 0
    grep -F -q "bootout $3" "$TEST_ROOT/events" && return 1
    return 0
  fi
  if [[ "$1" == rm ]]; then
    shift
    command rm "$@"
    return
  fi
  return 0
}
'''
        path = os.environ.get("PATH", "")
        return subprocess.run(
            ["/bin/bash", "-c", definitions + "\n" + harness + "\nmain " + " ".join(shlex.quote(a) for a in args)],
            env={
                "TEST_ROOT": str(self.root),
                "PATH": f"{self.stub_bin}:{path}",
                "HOME": str(self.home),
                **env,
            },
            text=True, capture_output=True, timeout=20,
        )

    def test_help_describes_keep_files(self):
        result = subprocess.run(
            [str(SCRIPT), "--help"], text=True, capture_output=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("USAGE", result.stderr)
        self.assertIn("--keep-files", result.stderr)
        self.assertIn("tool caches", result.stderr)

    def test_org_flags_are_validated(self):
        for args in (
            ["--keep-files", "--yes"],
            ["--keychain-item", "gh-runner-pat", "--yes"],
            ["--org", "acme/app", "--yes"],
            ["--org", "acme", "--token", "t", "--keychain-item", "gh-runner-pat"],
        ):
            result = subprocess.run(
                [str(SCRIPT), *args], text=True, capture_output=True, timeout=10,
            )
            self.assertEqual(result.returncode, 2, args)

    def test_default_cleanup_deletes_the_runner_and_deregisters_with_a_token(self):
        directory = self.runner(org="acme")
        result = self.invoke("--all", "--yes", RUNNER_REMOVE_TOKEN="removal-token")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("removed", result.stdout)
        self.assertEqual((self.root / "token-seen").read_text().strip(), "removal-token")
        self.assertFalse(directory.exists())
        self.assertFalse((self.plist_dir / "com.github.runner-1.plist").exists())
        events = (self.root / "events").read_text()
        self.assertLess(events.index("bootout system/com.github.runner-1"),
                        events.index(f"rm -rf {directory}"))

    def test_default_cleanup_ignores_org_and_active_job_checks(self):
        directory = self.runner(org="other")
        result = self.invoke(
            "--runner", "1", "--yes",
            FAKE_PS=str(directory / "bin" / "Runner.Worker"),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("NOT deregistered", result.stderr)
        self.assertFalse(directory.exists())

    def test_all_leaves_health_and_autoscale_services(self):
        self.runner(org="acme")
        services = []
        for label in ("com.github.runner-health", "com.github.runner-autoscale"):
            path = self.plist_dir / f"{label}.plist"
            with path.open("wb") as handle:
                plistlib.dump({"Label": label, "ProgramArguments": ["/bin/true"]}, handle)
            services.append(path)
        result = self.invoke("--all", "--yes", RUNNER_REMOVE_TOKEN="removal-token")
        self.assertEqual(result.returncode, 0, result.stderr)
        for path in services:
            self.assertTrue(path.exists(), path)
        self.assertNotIn("runner-health", result.stdout)

    def test_org_cleanup_without_keep_files_deletes_only_that_org(self):
        gone = self.runner("runner-1", org="acme")
        kept = self.runner("runner-2", org="other")
        self.token_stub()
        result = self.invoke("--org", "acme", "--yes")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / "token-seen").read_text().strip(), "minted-removal-token")
        self.assertFalse(gone.exists())
        self.assertTrue((kept / ".runner").exists())
        self.assertTrue((self.plist_dir / "com.github.runner-2.plist").exists())

    def test_deregister_removes_org_registration_and_keeps_the_install(self):
        directory = self.runner(org="acme")
        self.token_stub()
        result = self.invoke("--org", "acme", "--keep-files", "--yes")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("deregistered", result.stdout)
        self.assertNotIn("minted-removal-token", result.stderr + result.stdout)
        self.assertEqual((self.root / "token-seen").read_text().strip(), "minted-removal-token")
        self.assertEqual((self.root / "token-cmd").read_text().strip(), "removal --org acme")
        self.assertFalse((directory / ".runner").exists())
        self.assertFalse((directory / ".credentials").exists())
        self.assertEqual((directory / "_toolcache" / "keep").read_text(), "tool")
        self.assertEqual((directory / "_work" / "keep").read_text(), "checkout")
        self.assertEqual((directory / ".github_pat").read_text(), "pat-secret\n")
        self.assertTrue((directory / "config.sh").exists())
        self.assertFalse((self.plist_dir / "com.github.runner-1.plist").exists())
        events = (self.root / "events").read_text()
        self.assertIn("bootout system/com.github.runner-1", events)

    def test_other_org_and_repo_runners_are_left_alone(self):
        kept = self.runner("runner-1", org="other")
        repo = self.runner_root / "runner-2"
        (repo / "bin").mkdir(parents=True)
        (repo / ".runner").write_text(json.dumps({
            "agentName": "repo-runner",
            "gitHubUrl": "https://github.com/acme/app",
        }))
        (repo / "config.sh").write_text("#!/bin/bash\nexit 9\n")
        (repo / "config.sh").chmod(0o755)
        self.plist("runner-2", "acme")
        result = self.invoke("--org", "acme", "--keep-files", "--yes", RUNNER_REMOVE_TOKEN="should-not-be-used")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("skipped (registered to other)", result.stdout)
        self.assertIn("skipped (not an org runner)", result.stdout)
        self.assertTrue((kept / ".runner").exists())
        self.assertTrue((repo / ".runner").exists())
        self.assertTrue((self.plist_dir / "com.github.runner-1.plist").exists())
        self.assertFalse((self.root / "token-seen").exists())

    def test_busy_runner_is_not_stopped(self):
        directory = self.runner()
        result = self.invoke(
            "--org", "acme", "--keep-files", "--yes", "--token", "removal-token",
            FAKE_PS=str(directory / "bin" / "Runner.Worker"),
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("active job", result.stderr)
        self.assertTrue((directory / ".runner").exists())
        self.assertTrue((self.plist_dir / "com.github.runner-1.plist").exists())
        self.assertFalse((self.root / "token-seen").exists())

    def test_stuck_daemon_is_not_deregistered(self):
        directory = self.runner()
        result = self.invoke(
            "--org", "acme", "--keep-files", "--yes", "--token", "removal-token",
            STUCK_DAEMON="1",
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("still loaded", result.stderr)
        self.assertTrue((directory / ".runner").exists())
        self.assertTrue((self.plist_dir / "com.github.runner-1.plist").exists())
        self.assertFalse((self.root / "token-seen").exists())

    def test_failed_remove_leaves_the_registration_file(self):
        directory = self.runner(remove_rc=1)
        result = self.invoke("--org", "acme", "--keep-files", "--yes", "--token", "removal-token")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("FAILED (config.sh remove)", result.stdout)
        self.assertTrue((directory / ".runner").exists())
        self.assertTrue((directory / "_toolcache" / "keep").exists())

    def test_dry_run_and_missing_confirmation_change_nothing(self):
        directory = self.runner()
        self.token_stub()
        dry = self.invoke("--org", "acme", "--keep-files", "--dry-run")
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertIn("Would unregister", dry.stderr)
        self.assertTrue((directory / ".runner").exists())
        self.assertFalse((self.root / "token-cmd").exists())
        refused = self.invoke("--org", "acme", "--keep-files")
        self.assertEqual(refused.returncode, 1, refused.stderr)
        self.assertIn("--yes", refused.stderr)
        self.assertTrue((directory / ".runner").exists())
        self.assertTrue((self.plist_dir / "com.github.runner-1.plist").exists())

    def test_ephemeral_service_without_registration_is_unloaded(self):
        directory = self.runner_root / "runner-1"
        (directory / "_toolcache").mkdir(parents=True)
        (directory / "_toolcache" / "keep").write_text("tool")
        self.plist("runner-1", "acme")
        result = self.invoke("--org", "acme", "--keep-files", "--yes")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("unloaded (no local registration)", result.stdout)
        self.assertTrue((directory / "_toolcache" / "keep").exists())
        self.assertFalse((self.plist_dir / "com.github.runner-1.plist").exists())
        self.assertFalse((self.root / "token-cmd").exists())

    def test_health_plist_for_the_same_org_is_reported(self):
        self.runner()
        health = self.plist_dir / "com.github.runner-health.plist"
        with health.open("wb") as handle:
            plistlib.dump({
                "ProgramArguments": ["/opt/homebrew/bin/runner-health", "--once", "--org", "acme"],
            }, handle)
        result = self.invoke("--org", "acme", "--keep-files", "--yes", "--token", "removal-token")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("com.github.runner-health.plist still names --org acme", result.stderr)
        self.assertTrue(health.exists())
