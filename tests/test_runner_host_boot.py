"""Unattended-boot host steps with fake commands; never alter real host settings."""
import os
from pathlib import Path
import plistlib
import subprocess
import tempfile
import unittest

import test_runner_toolcache as setup_fixture

FAKES = {
    'fdesetup': '''echo "$*" >> "$TEST_ROOT/fdesetup-calls"
case "$1" in
  status) cat "$TEST_ROOT/filevault" ;;
  disable) exit "$(cat "$TEST_ROOT/disable-rc" 2>/dev/null || echo 0)" ;;
esac''',
    'arch': '[[ -f "$TEST_ROOT/rosetta" ]]',
    'softwareupdate': '''echo "$*" >> "$TEST_ROOT/softwareupdate-calls"
[[ -f "$TEST_ROOT/install-fails" ]] || touch "$TEST_ROOT/rosetta"''',
    'colima': 'cat "$TEST_ROOT/vm-rosetta" 2>/dev/null',
    'upgrade': 'echo "$*" >> "$TEST_ROOT/upgrade-calls"',
    'systemsetup': '''echo "$*" >> "$TEST_ROOT/systemsetup-calls"
[[ ! -f "$TEST_ROOT/fda-denied" ]] || { echo "requires Full Disk Access" >&2; exit 1; }
case "$1" in
  -getremotelogin) [[ -f "$TEST_ROOT/remotelogin" ]] && echo "Remote Login: On" || echo "Remote Login: Off" ;;
  -setremotelogin) [[ "$2" != on ]] || touch "$TEST_ROOT/remotelogin" ;;
esac''',
    'tailscale': '''echo "$*" >> "$TEST_ROOT/tailscale-calls"
while [[ "${1:-}" == --socket=* ]]; do shift; done
case "$1" in
  status) [[ -f "$TEST_ROOT/ts-connected" ]] ;;
  up) touch "$TEST_ROOT/ts-connected" ;;
esac''',
}


class HostBootTests(unittest.TestCase):
    setUp_fixture = setup_fixture.SetupToolCacheTests.setUp

    def setUp(self):
        self.setUp_fixture()
        bin_dir = self.root / 'bin'
        bin_dir.mkdir()
        for name, body in FAKES.items():
            path = bin_dir / name
            path.write_text('#!/bin/bash\n' + body + '\n')
            path.chmod(0o755)

    def shell(self, body, argv0='runner-setup'):
        return subprocess.run(
            ['/bin/bash', '-c', setup_fixture.definitions('runner-setup') + '\n'
             + setup_fixture.SetupToolCacheTests.HARNESS + '\n'
             + 'ps() { cat "$TEST_ROOT/ps" 2>/dev/null || echo /sbin/launchd; }\n' + body, argv0],
            env=dict(os.environ, TEST_ROOT=str(self.root),
                     PATH=f'{self.root/"bin"}:{os.environ["PATH"]}'),
            text=True, capture_output=True)

    def lines(self, name):
        path = self.root / name
        return path.read_text().splitlines() if path.exists() else []

    def test_missing_rosetta_is_installed_for_colima_hosts(self):
        result = self.shell('COLIMA=1; ARCH_TAG=arm64; ensure_host_rosetta')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.lines('softwareupdate-calls'), ['--install-rosetta --agree-to-license'])

    def test_active_rosetta_and_non_colima_hosts_install_nothing(self):
        (self.root/'rosetta').touch()
        self.assertEqual(self.shell('COLIMA=1; ARCH_TAG=arm64; ensure_host_rosetta').returncode, 0)
        (self.root/'rosetta').unlink()
        for body in ('COLIMA=0; ARCH_TAG=arm64; ensure_host_rosetta',
                     'COLIMA=1; ARCH_TAG=x64; ensure_host_rosetta',
                     'COLIMA=1; ARCH_TAG=arm64; RUNNER_ALLOW_QEMU=1 ensure_host_rosetta'):
            self.assertEqual(self.shell(body).returncode, 0)
        self.assertEqual(self.lines('softwareupdate-calls'), [])

    def test_unverified_rosetta_install_stops_setup(self):
        (self.root/'install-fails').touch()
        result = self.shell('COLIMA=1; ARCH_TAG=arm64; ensure_host_rosetta')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Rosetta 2 could not be installed', result.stderr)

    COLIMA = 'COLIMA=1; ARCH_TAG=arm64; COLIMA_CONFIG_HOME=/c; COLIMA_DOCKER_CONFIG=/d\n'

    def test_vm_without_rosetta_is_restarted(self):
        result = self.shell(self.COLIMA + 'restart_colima_without_rosetta')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('launchctl kickstart -k system/com.github.runner-colima', self.lines('sudo-calls'))

    def test_vm_with_rosetta_is_not_restarted(self):
        (self.root/'vm-rosetta').write_text('enabled\n')
        result = self.shell(self.COLIMA + 'restart_colima_without_rosetta')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any('kickstart' in call for call in self.lines('sudo-calls')))

    def test_vm_restart_waits_for_active_jobs(self):
        (self.root/'ps').write_text('/opt/github-runners/runner-1/bin/Runner.Worker\n')
        result = self.shell(self.COLIMA + 'restart_colima_without_rosetta')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Active runner job', result.stderr)
        self.assertFalse(any('kickstart' in call for call in self.lines('sudo-calls')))

    def test_host_flags_moved_to_bootstrap_warn_but_still_parse(self):
        result = self.shell('parse_args --org acme --token x --skip-power-config --keep-filevault '
                            '--skip-remote-login --skip-tailscale; echo "parsed $ORG"')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('parsed acme', result.stdout)
        self.assertEqual(result.stderr.count('now belongs to runner-bootstrap'), 3)
        self.assertIn('--keep-filevault is gone', result.stderr)

    def test_health_watchdog_daemon_is_installed_after_setup(self):
        health = self.root / 'bin/runner-health'
        health.write_text('#!/bin/bash\nexit 0\n')
        health.chmod(0o755)
        result = self.shell(f'RUNNER_HEALTH_LOGS="$TEST_ROOT/logs"; '
                            f'command() {{ [[ "$2" == runner-health ]] && echo "{health}"; }}; '
                            'install_health_watchdog')
        self.assertEqual(result.returncode, 0, result.stderr)
        plist = self.root / 'plists/com.github.runner-health.plist'
        with plist.open('rb') as f:
            config = plistlib.load(f)
        self.assertEqual(config['ProgramArguments'], [str(health), '--once', '--reboot-for-gui'])
        self.assertEqual(config['StartInterval'], 300)
        self.assertTrue(config['RunAtLoad'])
        self.assertNotIn('UserName', config)   # root: it reads the system domain and reboots
        self.assertEqual(config['StandardOutPath'], str(self.root / 'logs/stdout.log'))
        self.assertIn(f'chown root:wheel {plist}', self.lines('sudo-calls'))
        self.assertIn(f'chmod 644 {plist}', self.lines('sudo-calls'))
        self.assertIn('launchctl enable system/com.github.runner-health', self.lines('sudo-calls'))
        self.assertIn(f'launchctl bootstrap system {plist}', self.lines('sudo-calls'))
        self.assertIn('runs runner-health every 300s', result.stderr)

    def test_operator_health_plist_is_kept_and_only_loaded(self):
        plist = self.root / 'plists/com.github.runner-health.plist'
        with plist.open('wb') as f:
            plistlib.dump({'Label': 'com.github.runner-health',
                           'ProgramArguments': ['/opt/homebrew/bin/runner-health', '--once', '--org', 'acme'],
                           'EnvironmentVariables': {'GITHUB_PAT': 'secret'}}, f)
        before = plist.read_bytes()
        result = self.shell('install_health_watchdog')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(plist.read_bytes(), before)
        self.assertIn('keeping it', result.stderr)
        self.assertFalse(any(call.startswith('cp ') for call in self.lines('sudo-calls')))
        self.assertIn(f'launchctl bootstrap system {plist}', self.lines('sudo-calls'))
        # Already loaded: nothing is bootstrapped twice.
        (self.root / 'loaded-com.github.runner-health').touch()
        (self.root / 'sudo-calls').unlink()
        self.assertEqual(self.shell('install_health_watchdog').returncode, 0)
        self.assertFalse(any('bootstrap' in call for call in self.lines('sudo-calls')))

    def test_skip_health_installs_nothing(self):
        result = self.shell('parse_args --org acme --token x --skip-health; install_health_watchdog')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--skip-health', result.stderr)
        self.assertFalse((self.root / 'plists/com.github.runner-health.plist').exists())
        self.assertEqual(self.lines('sudo-calls'), [])

    def test_setup_finishes_by_starting_and_verifying_every_runner(self):
        result = self.shell('UPGRADE_BIN="$TEST_ROOT/bin/upgrade"; RUNNERS=2; DOCKER_TIMEOUT=120; start_runners')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.lines('upgrade-calls'),
                         ['start --runners 2 --yes --health-timeout 420'])

    BREW = '''
brew() {
  echo "$*" >> "$TEST_ROOT/brew-calls"
  case "$1" in
    --prefix) echo "$TEST_ROOT/prefix" ;;
    list) cat "$TEST_ROOT/version" ;;
    upgrade) echo "runner-setup 1.14.0" > "$TEST_ROOT/version" ;;
  esac
}
'''

    def installed_setup(self):
        installed = self.root/'prefix/bin/runner-setup'
        installed.parent.mkdir(parents=True)
        installed.write_text('#!/bin/bash\necho "rerun $RUNNER_SETUP_UPGRADED $*" > "$TEST_ROOT/rerun"\n')
        installed.chmod(0o755)
        (self.root/'version').write_text('runner-setup 1.13.3\n')
        return installed

    def test_homebrew_install_upgrades_and_reruns_with_the_same_arguments(self):
        installed = self.installed_setup()
        result = self.shell(self.BREW + 'ORIGINAL_ARGS=(--org acme --colima); self_upgrade; echo continued',
                            argv0=str(installed))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root/'rerun').read_text(), 'rerun 1 --org acme --colima\n')
        self.assertNotIn('continued', result.stdout)
        self.assertIn('upgrade joeblau/hb/runner-setup', self.lines('brew-calls'))

    def test_current_install_and_checkouts_continue_without_rerunning(self):
        installed = self.installed_setup()
        brew = self.BREW.replace('upgrade) echo "runner-setup 1.14.0" > "$TEST_ROOT/version" ;;', 'upgrade) ;;')
        for body, argv0 in ((brew, str(installed)), (self.BREW, 'runner-setup'),
                            (self.BREW + 'RUNNER_SETUP_UPGRADED=1; ', str(installed)),
                            (self.BREW + 'SELF_UPGRADE=0; ', str(installed))):
            result = self.shell(body + 'ORIGINAL_ARGS=(--org acme); self_upgrade; echo continued', argv0=argv0)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('continued', result.stdout)
        self.assertFalse((self.root/'rerun').exists())

    def test_failed_homebrew_upgrade_continues_with_installed_release(self):
        installed = self.installed_setup()
        brew = self.BREW.replace('upgrade) echo "runner-setup 1.14.0" > "$TEST_ROOT/version" ;;', 'update) return 1 ;;')
        result = self.shell(brew + 'ORIGINAL_ARGS=(--org acme); self_upgrade; echo continued', argv0=str(installed))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('continued', result.stdout)
        self.assertIn('Homebrew upgrade failed', result.stderr)


if __name__ == '__main__':
    unittest.main()
