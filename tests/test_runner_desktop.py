"""Bootstrap's inline desktop cleanup against temporary plists, never the user's UI."""

import os
from pathlib import Path
import plistlib
import pwd
import shlex
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "bin" / "runner-bootstrap"
# Exercise main(), stubbing only unrelated host changes. The real cleanup and
# verification run with macOS defaults redirected to temporary preference files.
HOST_STUBS = '''
ensure_sudo() { :; }
load_brew_env() { :; }
configure_power() { :; }
configure_unattended_preferences() { :; }
configure_filevault() { :; }
configure_remote_access() { :; }
configure_tailscale() { :; }
configure_tailscale_guard() { :; }
configure_auto_login() { :; }
verify_host() { check_desktop; }
'''
DOMAINS = ("com.apple.dock", "com.apple.WindowManager")
MOCK = r'''
import json, os, pathlib, subprocess, sys
root = pathlib.Path(os.environ['DESKTOP_TEST_ROOT'])
name, args = pathlib.Path(sys.argv[0]).name, sys.argv[1:]
with (root / 'calls.jsonl').open('a') as stream:
    stream.write(json.dumps([name] + args) + '\n')
if name == 'killall':
    sys.exit(1)  # Also exercise running when the user has no UI processes.
assert name == 'defaults', name
assert len(args) >= 2 and args[1] in ('com.apple.dock', 'com.apple.WindowManager'), args
domain = args[1]
if args[0] == 'export' and domain == os.environ.get('FAIL_EXPORT'):
    sys.exit(42)
if args[0] == 'export' and domain == os.environ.get('CORRUPT_EXPORT'):
    print('not a plist')
    sys.exit(0)
if args[0] == 'write' and args[2] == os.environ.get('FAIL_WRITE'):
    sys.exit(43)
args[1] = str(root / domain)
sys.exit(subprocess.run(['/usr/bin/defaults'] + args).returncode)
'''


@unittest.skipUnless(sys.platform == "darwin" and os.geteuid() != 0,
                     "Requires macOS defaults and a non-root user")
class BootstrapDesktopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="runner-desktop-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "bin").mkdir()
        (self.root / "home").mkdir()
        for name in ("defaults", "killall"):
            mock = self.root / "bin" / name
            mock.write_text(f"#!{sys.executable}\n" + MOCK)
            mock.chmod(0o755)
        self.original = {
            DOMAINS[0]: {
                "persistent-apps": [{"tile-data": {"file-label": "Safari"}}],
                "persistent-others": ["Downloads"],
                "static-apps": ["Terminal"], "static-others": ["Documents"],
                "show-recents": True, "tilesize": 48,
            },
            DOMAINS[1]: {
                "StandardHideWidgets": False, "StageManagerHideWidgets": False,
                "GloballyEnabled": True, "EnableTiledWindowMargins": False,
            },
        }
        for domain, prefs in self.original.items():
            (self.root / f"{domain}.plist").write_bytes(plistlib.dumps(prefs))
        self.env = dict(os.environ, HOME=str(self.root / "home"),
                        PATH=f"{self.root}/bin:/usr/bin:/bin",
                        DESKTOP_TEST_ROOT=str(self.root))
        self.backups = (self.root / "home/Library/Application Support/clear-mac/backups")

    def run_script(self, *args, postscript="", **env):
        body = SCRIPT.read_text().rsplit('main "$@"', 1)[0] + HOST_STUBS + '\nmain "$@"\n' + postscript
        return subprocess.run(['/bin/bash', '-c', body, 'runner-bootstrap', '--skip-brew', *args],
                              env=dict(self.env, **env), capture_output=True, text=True, timeout=15)

    def read_prefs(self, domain):
        result = subprocess.run(["/usr/bin/defaults", "export", str(self.root / domain), "-"],
                                capture_output=True, check=True, timeout=5)
        return plistlib.loads(result.stdout)

    def calls(self):
        import json
        path = self.root / "calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def assert_no_changes(self):
        for domain in DOMAINS:
            self.assertEqual(self.read_prefs(domain), self.original[domain])
        self.assertFalse(any(call[0] == "killall" or call[1] == "write" for call in self.calls()))

    def test_clear_preserves_other_settings_and_backs_up_both_domains(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        dock = self.read_prefs(DOMAINS[0])
        self.assertEqual(dock, {**self.original[DOMAINS[0]],
                              **dict.fromkeys(("persistent-apps", "persistent-others",
                                               "static-apps", "static-others"), []),
                              "show-recents": False})
        self.assertEqual(self.read_prefs(DOMAINS[1]),
                         {**self.original[DOMAINS[1]], "StandardHideWidgets": True,
                          "StageManagerHideWidgets": True})
        backup, = self.backups.iterdir()
        self.assertEqual(backup.stat().st_mode & 0o777, 0o700)
        for domain in DOMAINS:
            path = backup / f"{domain}.plist"
            self.assertEqual(plistlib.loads(path.read_bytes()), self.original[domain])
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        calls = self.calls()
        exports = [i for i, call in enumerate(calls) if call[1] == "export"]
        first_write = next(i for i, call in enumerate(calls) if call[1] == "write")
        self.assertEqual(len(exports), 2)
        self.assertLess(max(exports), first_write)
        self.assertEqual([call for call in calls if call[0] == "killall"],
                         [["killall", "-u", pwd.getpwuid(os.geteuid()).pw_name,
                           "Dock", "NotificationCenter"]])
        self.assertIn("saved layouts are preserved", result.stderr)
        self.assertRegex(result.stderr, r'Dock and desktop\s+ok')

    def test_widget_backup_failure_leaves_both_domains_unchanged(self):
        result = self.run_script(FAIL_EXPORT=DOMAINS[1])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("stopping before changing preferences", result.stderr)
        self.assert_no_changes()

    def test_invalid_backup_is_rejected_before_changes(self):
        result = self.run_script(CORRUPT_EXPORT=DOMAINS[0])
        self.assertNotEqual(result.returncode, 0)
        self.assert_no_changes()

    def test_fresh_account_without_widget_preferences(self):
        (self.root / f"{DOMAINS[1]}.plist").unlink()
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.read_prefs(DOMAINS[1]),
                         {"StandardHideWidgets": True, "StageManagerHideWidgets": True})
        backup, = self.backups.iterdir()
        self.assertEqual(plistlib.loads((backup / f"{DOMAINS[1]}.plist").read_bytes()), {})

    def test_printed_restore_commands_restore_original_preferences(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        commands = [line.removeprefix("Restore with: ") for line in result.stderr.splitlines()
                    if line.startswith("Restore with: ")]
        self.assertEqual(len(commands), 2)
        for command in commands:
            subprocess.run(shlex.split(command), env=self.env, check=True, timeout=5,
                           capture_output=True)
        for domain in DOMAINS:
            self.assertEqual(self.read_prefs(domain), self.original[domain])

    def test_write_failure_retains_backups_and_reports_no_success(self):
        result = self.run_script(FAIL_WRITE="StandardHideWidgets")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Restore with:", result.stderr)
        self.assertNotIn("Cleared removable", result.stderr)
        self.assertNotIn("This Mac is ready", result.stderr)
        backup, = self.backups.iterdir()
        for domain in DOMAINS:
            self.assertEqual(plistlib.loads((backup / f"{domain}.plist").read_bytes()),
                             self.original[domain])

    def test_help_and_invalid_arguments_do_not_change_preferences(self):
        result = self.run_script("--help")
        self.assertEqual(result.returncode, 0)
        self.assertIn("USAGE:", result.stderr)
        self.assertEqual(self.run_script("unknown").returncode, 2)
        self.assert_no_changes()
        self.assertFalse(self.backups.exists())

    def test_skip_and_check_never_change_preferences(self):
        for flag in ("--skip-clear-desktop", "--check"):
            with self.subTest(flag=flag):
                result = self.run_script(flag)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assert_no_changes()
                self.assertFalse(self.backups.exists())

    def test_repeated_bootstrap_does_not_backup_or_refresh_again(self):
        first = self.run_script()
        self.assertEqual(first.returncode, 0, first.stderr)
        second = self.run_script()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("already clear", second.stderr)
        self.assertEqual(len(list(self.backups.iterdir())), 1)
        self.assertEqual(sum(call[0] == "killall" for call in self.calls()), 1)

    def test_cleanup_never_invokes_a_legacy_clear_mac_executable(self):
        legacy = self.root / "bin/clear-mac"
        legacy.write_text('#!/bin/bash\necho LEGACY_HELPER_CALLED >&2\nexit 99\n')
        legacy.chmod(0o755)
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("LEGACY_HELPER_CALLED", result.stderr)
        self.assertEqual(self.read_prefs(DOMAINS[0])["persistent-apps"], [])

    def test_backup_permissions_do_not_change_bootstraps_umask(self):
        expected = subprocess.check_output(['/bin/bash', '-c', 'umask'], text=True)
        result = self.run_script(postscript='umask')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, expected)


if __name__ == "__main__":
    unittest.main()
