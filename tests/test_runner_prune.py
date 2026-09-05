"""Exercise real cleanup against isolated files; never touch installed runners."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "runner-prune"


class PruneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.fleet = self.root / "fleet"
        self.home = self.root / "home"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for index in (1, 2):
            for directory in ("repo", "_tool", "_actions", "_temp", ".hidden-output"):
                self.touch(self.fleet / f"runner-{index}" / "_work" / directory / "keep")
        self.touch(self.home / ".npm" / "keep")
        for name, body in {
            "uname": "echo Darwin",
            "whoami": "echo runner-test",
            "dscl": f'echo "NFSHomeDirectory: {self.home}"',
            "df": 'echo "Filesystem 1024-blocks Used Available Capacity Mounted"; echo "disk 999999999 1 ${TEST_FREE_KB:-999999999} 1% /"',
            "sudo": "exit 1",
        }.items():
            p = self.bin / name
            p.write_text("#!/bin/bash\n" + body + "\n")
            p.chmod(0o755)
        self.env = dict(os.environ, PATH=f"{self.bin}:/usr/bin:/bin", RUNNER_PRUNE_DOCKER="0")
        for key in ("RUNNER_TEMP", "GITHUB_ACTIONS", "SUDO_USER"):
            self.env.pop(key, None)

    def touch(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("valuable cached or job data")

    def run_prune(self, *args, **env):
        return subprocess.run(["/bin/bash", str(SCRIPT), "--runners-dir", str(self.fleet), *args],
                              env=dict(self.env, **env), capture_output=True, text=True)

    def test_hook_cleans_only_own_checkout_and_keeps_caches(self):
        result = self.run_prune(RUNNER_TEMP=str(self.fleet / "runner-1/_work/_temp"), GITHUB_ACTIONS="true")
        self.assertEqual(result.returncode, 0, result.stderr)
        work = self.fleet / "runner-1/_work"
        self.assertFalse((work / "repo").exists())
        self.assertFalse((work / ".hidden-output").exists())
        for name in ("_tool", "_actions", "_temp"):
            self.assertTrue((work / name / "keep").exists())
        self.assertTrue((self.fleet / "runner-2/_work/repo/keep").exists())
        self.assertTrue((self.home / ".npm/keep").exists())

    def test_unscoped_invocation_fails_without_deleting(self):
        self.assertEqual(self.run_prune().returncode, 2)
        self.assertTrue((self.fleet / "runner-1/_work/repo/keep").exists())

    def test_fleet_cleanup_rejected_inside_hook(self):
        result = self.run_prune("--all", "--offline", GITHUB_ACTIONS="true")
        self.assertEqual(result.returncode, 2)
        self.assertTrue((self.fleet / "runner-1/_work/repo/keep").exists())

    def test_hook_cannot_clean_neighbor(self):
        result = self.run_prune("--runner-dir", str(self.fleet / "runner-2"),
                                RUNNER_TEMP=str(self.fleet / "runner-1/_work/_temp"))
        self.assertEqual(result.returncode, 1)
        self.assertTrue((self.fleet / "runner-2/_work/repo/keep").exists())

    def test_offline_shared_cache_eviction(self):
        result = self.run_prune("--all", "--offline", "--purge-caches")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.home / ".npm").exists())
        self.assertFalse((self.fleet / "runner-2/_work/repo").exists())

    def test_symlinked_ancestor_is_canonicalized(self):
        alias = self.root / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        result = self.run_prune("--runners-dir", str(alias / "fleet"),
                                RUNNER_TEMP=str(alias / "fleet/runner-1/_work/_temp"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.fleet / "runner-1/_work/repo").exists())
        self.assertTrue((self.fleet / "runner-2/_work/repo/keep").exists())

    def test_symlinked_runner_rejected(self):
        (self.fleet / "runner-3").symlink_to(self.fleet / "runner-2", target_is_directory=True)
        result = self.run_prune("--runner-dir", str(self.fleet / "runner-3"))
        self.assertEqual(result.returncode, 1)
        self.assertTrue((self.fleet / "runner-2/_work/repo/keep").exists())

    def test_low_disk_returns_signal_and_dry_run_keeps_files(self):
        args = ("--runner-dir", str(self.fleet / "runner-1"))
        result = self.run_prune(*args, "--dry-run", TEST_FREE_KB="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.fleet / "runner-1/_work/repo/keep").exists())
        self.assertEqual(self.run_prune(*args, TEST_FREE_KB="1").returncode, 75)


if __name__ == "__main__":
    unittest.main()
