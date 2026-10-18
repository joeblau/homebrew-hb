"""runner-bootstrap host setup with fake commands; never alters real host settings.

Covers the host steps moved out of runner-setup (FileVault, Remote Login,
Tailscale and its panic guard) and bootstrap's own steps: automatic login,
clearing the Dock and desktop, the Homebrew/Brewfile install, the hand-off
to the installed release, and the verification table.
"""
import os
from pathlib import Path
import plistlib
import subprocess
import unittest

import test_runner_toolcache as setup_fixture
from test_runner_host_boot import FAKES

BOOTSTRAP_FAKES = dict(FAKES, **{
    # Fake preference store: $TEST_ROOT/defaults/<domain>/<key> holds the
    # value `defaults read` prints; a missing file is a missing key.
    'defaults': '''echo "$*" >> "$TEST_ROOT/defaults-calls"
case "$1" in
  read) cat "$TEST_ROOT/defaults/${2##*/}/$3" 2>/dev/null ;;
  *) exit 0 ;;
esac''',
    'clear-mac': 'echo cleared >> "$TEST_ROOT/clear-mac-calls"',
    'sysadminctl': 'echo "$*" >> "$TEST_ROOT/sysadminctl-calls"',
})

CLEAR = {
    ('com.apple.dock', 'persistent-apps'): '(\n)',
    ('com.apple.dock', 'persistent-others'): '(\n)',
    ('com.apple.dock', 'show-recents'): '0',
    ('com.apple.WindowManager', 'StandardHideWidgets'): '1',
    ('com.apple.WindowManager', 'StageManagerHideWidgets'): '1',
}


class BootstrapTests(unittest.TestCase):
    setUp_fixture = setup_fixture.SetupToolCacheTests.setUp

    def setUp(self):
        self.setUp_fixture()
        bin_dir = self.root / 'bin'
        bin_dir.mkdir()
        for name, body in BOOTSTRAP_FAKES.items():
            path = bin_dir / name
            path.write_text('#!/bin/bash\n' + body + '\n')
            path.chmod(0o755)

    def shell(self, body, **env):
        return subprocess.run(
            ['/bin/bash', '-c', setup_fixture.definitions('runner-bootstrap') + '\n'
             + setup_fixture.SetupToolCacheTests.HARNESS + '\n' + body, 'runner-bootstrap'],
            env=dict(os.environ, TEST_ROOT=str(self.root), HOME=str(self.root / 'home'),
                     PATH=f'{self.root/"bin"}:{os.environ["PATH"]}', **env),
            text=True, capture_output=True, timeout=20)

    def lines(self, name):
        path = self.root / name
        return path.read_text().splitlines() if path.exists() else []

    def set_pref(self, domain, key, value):
        path = self.root / 'defaults' / domain / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value + '\n')

    def clear_desktop(self):
        for (domain, key), value in CLEAR.items():
            self.set_pref(domain, key, value)

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
        result = self.shell('parse_args --keep-filevault; configure_filevault')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.lines('fdesetup-calls'), [])

    def test_filevault_disable_failure_stops_setup(self):
        (self.root/'filevault').write_text('FileVault is On.\n')
        (self.root/'disable-rc').write_text('1')
        result = self.shell('configure_filevault')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('--keep-filevault', result.stderr)

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
        result = self.shell('parse_args --skip-remote-login; configure_remote_access')
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
        result = self.shell(self.TAILSCALE_BREW + 'parse_args --skip-tailscale; configure_tailscale')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.lines('brew-calls'), [])
        self.assertEqual(self.lines('tailscale-calls'), [])

    # configure_tailscale_guard looks beside its own script first; sourced
    # definitions have no script path, so the guard is found on PATH instead.
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
                            + 'parse_args --skip-tailscale; configure_tailscale_guard')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root / 'plists/com.github.runner-tailscale-guard.plist').exists())

    def test_missing_guard_binary_warns_and_continues(self):
        result = self.shell(self.GUARD_ENV
                            + 'PATH="/usr/bin:/bin:/usr/sbin"; configure_tailscale_guard; echo continued')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('skipping the panic guard', result.stderr)
        self.assertIn('continued', result.stdout)

    # --- bootstrap-only behavior ------------------------------------------

    def test_auto_login_conflicts_with_keep_filevault(self):
        result = self.shell('parse_args --auto-login --keep-filevault')
        self.assertEqual(result.returncode, 2)
        self.assertIn('FileVault', result.stderr)

    def test_runner_options_are_rejected(self):
        result = self.shell('parse_args --org acme')
        self.assertEqual(result.returncode, 2)
        self.assertIn('Unknown argument: --org', result.stderr)

    def test_auto_login_is_off_unless_requested(self):
        result = self.shell('configure_auto_login')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.lines('sysadminctl-calls'), [])

    def test_auto_login_needs_filevault_off(self):
        (self.root/'filevault').write_text('FileVault is On.\nDecryption in progress\n')
        result = self.shell('AUTO_LOGIN=1; configure_auto_login')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('reports Off', result.stderr)
        self.assertEqual(self.lines('sysadminctl-calls'), [])

    def test_auto_login_without_a_tty_asks_for_an_interactive_rerun(self):
        (self.root/'filevault').write_text('FileVault is Off.\n')
        result = self.shell('AUTO_LOGIN=1; configure_auto_login')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('re-run interactively', result.stderr)
        self.assertEqual(self.lines('sysadminctl-calls'), [])

    def test_auto_login_already_on_is_left_alone(self):
        self.set_pref('com.apple.loginwindow', 'autoLoginUser', 'fixture')
        (self.root/'kcpassword').write_text('x')
        result = self.shell('AUTO_LOGIN=1; configure_auto_login',
                            RUNNER_BOOTSTRAP_KCPASSWORD=str(self.root/'kcpassword'))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Automatic login is on for fixture', result.stderr)
        self.assertEqual(self.lines('sysadminctl-calls'), [])

    def test_cluttered_desktop_is_cleared_once(self):
        result = self.shell('configure_desktop')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.lines('clear-mac-calls'), ['cleared'])

    def test_clear_desktop_is_left_alone(self):
        self.clear_desktop()
        result = self.shell('configure_desktop')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('already clear', result.stderr)
        self.assertEqual(self.lines('clear-mac-calls'), [])

    def test_recent_apps_still_shown_is_not_clear(self):
        self.clear_desktop()
        self.set_pref('com.apple.dock', 'show-recents', '1')
        self.assertNotEqual(self.shell('desktop_is_clear').returncode, 0)

    def test_skip_clear_desktop_never_runs_clear_mac(self):
        result = self.shell('parse_args --skip-clear-desktop; configure_desktop')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.lines('clear-mac-calls'), [])

    BREW = '''
brew() {
  echo "$*" >> "$TEST_ROOT/brew-calls"
  case "$1" in
    --repository) echo "$TEST_ROOT/tap" ;;
    --prefix) echo "$TEST_ROOT/prefix" ;;
    shellenv) echo 'export HOMEBREW_PREFIX=/opt/homebrew' ;;
    bundle) [[ "$2" != check ]] || [[ -f "$TEST_ROOT/bundled" ]] ;;
  esac
}
brew_path() { echo /opt/homebrew/bin/brew; }
'''

    def test_brewfile_is_tapped_and_bundled(self):
        (self.root/'tap').mkdir()
        (self.root/'tap/Brewfile').write_text('brew "jq"\n')
        result = self.shell(self.BREW + 'install_brewfile')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.lines('brew-calls'),
                         ['tap joeblau/hb', '--repository joeblau/hb',
                          f'bundle --file={self.root}/tap/Brewfile'])

    def test_zprofile_gets_brew_shellenv_exactly_once(self):
        (self.root/'home').mkdir()
        body = self.BREW + 'load_brew_env() { :; }\ninstall_homebrew; install_homebrew'
        result = self.shell(body)
        self.assertEqual(result.returncode, 0, result.stderr)
        profile = (self.root/'home/.zprofile').read_text()
        self.assertEqual(profile.count('brew shellenv'), 1)

    def installed(self):
        path = self.root/'prefix/bin/runner-bootstrap'
        path.parent.mkdir(parents=True)
        path.write_text('#!/bin/bash\necho "$RUNNER_BOOTSTRAP_REEXEC $*" > "$TEST_ROOT/reexec"\n')
        path.chmod(0o755)

    def test_a_curl_copy_hands_over_to_the_installed_release(self):
        self.installed()
        result = self.shell(self.BREW + 'ORIGINAL_ARGS=(--auto-login); reexec_installed; echo continued')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root/'reexec').read_text(), '1 --auto-login --skip-brew\n')
        self.assertNotIn('continued', result.stdout)

    def test_handover_with_no_arguments_survives_bash_3_nounset(self):
        self.installed()
        result = self.shell(self.BREW + 'ORIGINAL_ARGS=(); reexec_installed')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root/'reexec').read_text(), '1 --skip-brew\n')

    def test_the_handed_over_release_does_not_hand_over_again(self):
        self.installed()
        result = self.shell(self.BREW + 'reexec_installed; echo continued',
                            RUNNER_BOOTSTRAP_REEXEC='1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('continued', result.stdout)
        self.assertFalse((self.root/'reexec').exists())

    def test_check_table_fails_on_a_required_item(self):
        (self.root/'filevault').write_text('FileVault is On.\n')
        result = self.shell('check_filevault; check_desktop; print_checks')
        self.assertNotEqual(result.returncode, 0)
        self.assertRegex(result.stderr, r'FileVault\s+FAIL')
        self.assertRegex(result.stderr, r'Dock and desktop\s+warn')

    def test_check_table_passes_when_only_warnings_remain(self):
        (self.root/'filevault').write_text('FileVault is Off.\n')
        result = self.shell('check_filevault; check_auto_login; check_desktop; print_checks')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('This Mac is ready', result.stderr)

    def test_requested_auto_login_that_is_off_fails_the_check(self):
        result = self.shell('AUTO_LOGIN=1; check_auto_login; print_checks')
        self.assertNotEqual(result.returncode, 0)
        self.assertRegex(result.stderr, r'Automatic login\s+FAIL')


if __name__ == '__main__':
    unittest.main()
