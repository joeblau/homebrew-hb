"""Unattended-boot host steps with fake commands; never alter real host settings."""
import os
from pathlib import Path
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

    def test_filevault_on_is_turned_off(self):
        (self.root/'filevault').write_text('FileVault is On.\n')
        result = self.shell('configure_filevault')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.lines('fdesetup-calls'), ['status', 'disable'])
        self.assertIn('fdesetup disable', self.lines('sudo-calls'))

    def test_filevault_off_or_decrypting_is_left_alone(self):
        for status in ('FileVault is Off.\n',
                       'FileVault is On.\nDecryption in progress: Percent completed = 12.0\n'):
            (self.root/'fdesetup-calls').unlink(missing_ok=True)
            (self.root/'filevault').write_text(status)
            result = self.shell('configure_filevault')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(self.lines('fdesetup-calls'), ['status'])

    def test_keep_filevault_skips_every_filevault_command(self):
        (self.root/'filevault').write_text('FileVault is On.\n')
        result = self.shell('parse_args --org acme --token x --keep-filevault; configure_filevault')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.lines('fdesetup-calls'), [])

    def test_filevault_disable_failure_stops_setup(self):
        (self.root/'filevault').write_text('FileVault is On.\n')
        (self.root/'disable-rc').write_text('1')
        result = self.shell('configure_filevault')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('--keep-filevault', result.stderr)

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

    def test_remote_login_is_enabled_and_verified(self):
        result = self.shell('configure_remote_access')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Enabled Remote Login', result.stderr)
        self.assertEqual(self.lines('systemsetup-calls'),
                         ['-getremotelogin', '-setremotelogin on', '-getremotelogin'])

    def test_remote_login_already_on_is_left_alone(self):
        (self.root/'remotelogin').touch()
        result = self.shell('configure_remote_access')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.lines('systemsetup-calls'), ['-getremotelogin'])

    def test_remote_login_without_full_disk_access_warns_and_continues(self):
        (self.root/'fda-denied').touch()
        result = self.shell('configure_remote_access; echo continued')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Full Disk Access', result.stderr)
        self.assertIn('continued', result.stdout)

    def test_skip_remote_login_makes_no_queries(self):
        result = self.shell('parse_args --org acme --token x --skip-remote-login; configure_remote_access')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.lines('systemsetup-calls'), [])

    TAILSCALE_BREW = ('brew() { echo "$*" >> "$TEST_ROOT/brew-calls"; }\n'
                      'sleep() { :; }\n')

    def test_tailscale_service_is_started_and_login_warned_without_a_tty(self):
        result = self.shell(self.TAILSCALE_BREW + 'configure_tailscale')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('services start tailscale', self.lines('brew-calls'))
        self.assertIn('sudo tailscale --socket=/var/run/tailscaled.socket up', result.stderr)
        self.assertEqual(self.lines('tailscale-calls'),
                         ['--socket=/var/run/tailscaled.socket status'])

    def test_login_only_tailscale_agent_is_replaced_with_the_boot_service(self):
        agent = self.root/'home/Library/LaunchAgents/homebrew.mxcl.tailscale.plist'
        agent.parent.mkdir(parents=True)
        agent.write_text('login-only')
        result = self.shell(self.TAILSCALE_BREW + 'configure_tailscale')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(agent.exists())
        self.assertIn('services stop tailscale', self.lines('brew-calls'))
        self.assertIn('services start tailscale', self.lines('brew-calls'))

    def test_connected_tailscale_with_loaded_service_is_left_alone(self):
        (self.root/'ts-connected').touch()
        (self.root/'loaded-homebrew.mxcl.tailscale').touch()
        result = self.shell(self.TAILSCALE_BREW + 'configure_tailscale')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Tailscale boot service is connected', result.stderr)
        self.assertEqual(self.lines('brew-calls'), [])

    def test_running_menu_bar_app_is_called_out(self):
        result = self.shell(self.TAILSCALE_BREW + 'pgrep() { [[ "$2" == Tailscale ]]; }\nconfigure_tailscale')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('menu-bar app is running', result.stderr)

    def test_skip_tailscale_does_nothing(self):
        result = self.shell(self.TAILSCALE_BREW + 'parse_args --org acme --token x --skip-tailscale; configure_tailscale')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.lines('brew-calls'), [])
        self.assertEqual(self.lines('tailscale-calls'), [])

    # configure_tailscale_guard resolves the guard next to runner-setup via
    # BASH_SOURCE, which in these sourced-definition tests is the bash binary's
    # directory; give it an explicit PATH hit instead.
    GUARD_ENV = ('plists_dir="$LAUNCH_DAEMON_DIR"; mkdir -p "$plists_dir"\n'
                 'export RUNNER_TS_GUARD_LOGS="$TEST_ROOT/guard-logs"\n')

    def guard_plist(self):
        import plistlib
        path = self.root / 'plists/com.github.runner-tailscale-guard.plist'
        self.assertTrue(path.exists())
        with open(path, 'rb') as f:
            return plistlib.load(f)

    def test_tailscale_guard_daemon_is_installed_and_loaded(self):
        guard = self.root / 'bin/runner-tailscale-guard'
        guard.write_text('#!/bin/bash\n')
        guard.chmod(0o755)
        result = self.shell(self.GUARD_ENV + 'configure_tailscale_guard')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Tailscale panic guard installed', result.stderr)
        config = self.guard_plist()
        self.assertEqual(config['ProgramArguments'], [str(guard), 'check'])
        self.assertTrue(config['RunAtLoad'])
        self.assertEqual(config['StartInterval'], 1800)
        self.assertNotIn('UserName', config)  # reboots need root
        self.assertTrue((self.root / 'loaded-com.github.runner-tailscale-guard').exists())

    def test_tailscale_guard_rerun_leaves_a_loaded_daemon_alone(self):
        guard = self.root / 'bin/runner-tailscale-guard'
        guard.write_text('#!/bin/bash\n')
        guard.chmod(0o755)
        first = self.shell(self.GUARD_ENV + 'configure_tailscale_guard')
        self.assertEqual(first.returncode, 0, first.stderr)
        plist = self.root / 'plists/com.github.runner-tailscale-guard.plist'
        before = plist.read_bytes()
        (self.root / 'sudo-calls').unlink()
        rerun = self.shell(self.GUARD_ENV + 'configure_tailscale_guard')
        self.assertEqual(rerun.returncode, 0, rerun.stderr)
        self.assertEqual(plist.read_bytes(), before)
        # An unchanged plist must not bounce the daemon.
        self.assertNotIn('bootout system/com.github.runner-tailscale-guard',
                         '\n'.join(self.lines('sudo-calls')))

    def test_skip_tailscale_skips_the_guard_too(self):
        result = self.shell(self.GUARD_ENV
                            + 'parse_args --org acme --token x --skip-tailscale; configure_tailscale_guard')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root / 'plists/com.github.runner-tailscale-guard.plist').exists())

    def test_missing_guard_binary_warns_and_continues(self):
        result = self.shell(self.GUARD_ENV
                            + 'PATH="/usr/bin:/bin:/usr/sbin"; configure_tailscale_guard; echo continued')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('skipping the panic guard', result.stderr)
        self.assertIn('continued', result.stdout)

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
