"""runner-bootstrap host setup with fake commands; never alters real host settings.

Covers the host steps moved out of runner-setup (FileVault, Remote Login,
Tailscale and its panic guard) and bootstrap's own steps: unattended preferences, automatic login,
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
  write)
    [[ ! -f "$TEST_ROOT/defaults-fail-$3" ]] || exit 1
    [[ ! -f "$TEST_ROOT/defaults-ignore-writes" ]] || exit 0
    mkdir -p "$TEST_ROOT/defaults/${2##*/}"
    value="$5"
    [[ "$4" != -bool || "$value" != false ]] || value=0
    echo "$value" > "$TEST_ROOT/defaults/${2##*/}/$3"
    ;;
  *) exit 0 ;;
esac''',
    'sysadminctl': 'echo "$*" >> "$TEST_ROOT/sysadminctl-calls"',
    # gui/<uid> exists only while that user is logged in at the console.
    'launchctl': '''echo "$*" >> "$TEST_ROOT/launchctl-calls"
case "$1 $2" in
  "print gui/501") [[ -f "$TEST_ROOT/gui-session" ]] ;;
  *) exit 0 ;;
esac''',
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

    def test_keep_filevault_is_no_longer_an_option(self):
        result = self.shell('parse_args --keep-filevault')
        self.assertEqual(result.returncode, 2)
        self.assertIn('Unknown argument: --keep-filevault', result.stderr)

    def test_filevault_disable_failure_stops_setup(self):
        (self.root/'filevault').write_text('FileVault is On.\n')
        (self.root/'disable-rc').write_text('1')
        result = self.shell('configure_filevault')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Could not turn off FileVault', result.stderr)

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

    # Homebrew has labelled services sh.brew.<formula> since September 2026.
    def test_current_homebrew_label_counts_as_loaded(self):
        (self.root/'ts-connected').touch()
        (self.root/'loaded-sh.brew.tailscale').touch()
        result = self.shell(self.TAILSCALE_BREW + 'configure_tailscale')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('already loaded (sh.brew.tailscale)', result.stderr)
        self.assertEqual(self.lines('brew-calls'), [])

    def test_current_homebrew_login_agent_is_replaced(self):
        agent = self.root/'home/Library/LaunchAgents/sh.brew.tailscale.plist'
        agent.parent.mkdir(parents=True)
        agent.write_text('login-only')
        result = self.shell(self.TAILSCALE_BREW + 'configure_tailscale')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(agent.exists())
        self.assertIn('services start tailscale', self.lines('brew-calls'))

    def test_check_passes_for_a_current_homebrew_boot_service(self):
        (self.root/'ts-connected').touch()
        for label in ('sh.brew.tailscale', 'com.github.runner-tailscale-guard'):
            (self.root/f'loaded-{label}').touch()
        result = self.shell('pgrep() { return 1; }; check_tailscale; print_checks')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stderr, r'Tailscale boot service\s+ok')

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

    def test_unattended_preferences_are_written_and_verified(self):
        self.set_pref('.GlobalPreferences', 'com.apple.autologout.AutoLogOutDelay', '300')
        self.set_pref('com.apple.SoftwareUpdate', 'AutomaticallyInstallMacOSUpdates', '1')
        result = self.shell('configure_unattended_preferences; check_unattended_preferences; print_checks')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.lines('sudo-calls'), [
            'defaults write /Library/Preferences/.GlobalPreferences '
            'com.apple.autologout.AutoLogOutDelay -int 0',
            'defaults write /Library/Preferences/com.apple.SoftwareUpdate '
            'AutomaticallyInstallMacOSUpdates -bool false',
        ])
        self.assertRegex(result.stderr, r'Automatic logout\s+ok')
        self.assertRegex(result.stderr, r'Automatic macOS updates\s+ok')

    def test_unattended_preference_write_failures_stop_setup(self):
        for key in ('com.apple.autologout.AutoLogOutDelay', 'AutomaticallyInstallMacOSUpdates'):
            with self.subTest(key=key):
                marker = self.root / f'defaults-fail-{key}'
                marker.touch()
                result = self.shell('configure_unattended_preferences')
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('bootstrap stopped', result.stderr)
                marker.unlink()

    def test_unattended_preferences_must_read_back_correctly(self):
        (self.root / 'defaults-ignore-writes').touch()
        result = self.shell('configure_unattended_preferences; check_unattended_preferences; print_checks')
        self.assertNotEqual(result.returncode, 0)
        self.assertRegex(result.stderr, r'Automatic logout\s+FAIL')
        self.assertRegex(result.stderr, r'Automatic macOS updates\s+FAIL')

    def test_missing_or_enabled_unattended_preferences_fail_without_writes(self):
        for value in (None, '1'):
            with self.subTest(value=value):
                if value is not None:
                    self.set_pref('.GlobalPreferences', 'com.apple.autologout.AutoLogOutDelay', value)
                    self.set_pref('com.apple.SoftwareUpdate', 'AutomaticallyInstallMacOSUpdates', value)
                result = self.shell('check_unattended_preferences; print_checks')
                self.assertNotEqual(result.returncode, 0)
                self.assertRegex(result.stderr, r'Automatic logout\s+FAIL')
                self.assertRegex(result.stderr, r'Automatic macOS updates\s+FAIL')
        self.assertFalse(any(call.startswith('write ') for call in self.lines('defaults-calls')))
        self.assertEqual(self.lines('sudo-calls'), [])

    HOST_STUBS = '''
detect_user() { :; }
ensure_sudo() { :; }
load_brew_env() { :; }
configure_power() { :; }
configure_filevault() { :; }
configure_remote_access() { :; }
configure_tailscale() { :; }
configure_tailscale_guard() { :; }
configure_auto_login() { :; }
configure_desktop() { :; }
check_brew() { :; }
check_power() { :; }
check_filevault() { :; }
check_remote_login() { :; }
check_tailscale() { :; }
check_auto_login() { :; }
check_console_session() { :; }
check_desktop() { :; }
check_rosetta() { :; }
'''

    def test_main_applies_unattended_preferences(self):
        result = self.shell(self.HOST_STUBS + 'main --skip-brew')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stderr, r'Automatic logout\s+ok')
        self.assertRegex(result.stderr, r'Automatic macOS updates\s+ok')
        self.assertEqual(len(self.lines('sudo-calls')), 2)

    def test_main_check_reports_missing_preferences_without_changing_them(self):
        result = self.shell(self.HOST_STUBS + 'main --check')
        self.assertNotEqual(result.returncode, 0)
        self.assertRegex(result.stderr, r'Automatic logout\s+FAIL')
        self.assertRegex(result.stderr, r'Automatic macOS updates\s+FAIL')
        self.assertFalse(any(call.startswith('write ') for call in self.lines('defaults-calls')))
        self.assertEqual(self.lines('sudo-calls'), [])

    def test_console_session_present_passes(self):
        (self.root / 'gui-session').touch()
        result = self.shell('check_console_session; print_checks')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stderr, r'Console session\s+ok\s+gui/501 \(fixture\)')
        self.assertEqual(self.lines('launchctl-calls'), ['print gui/501'])

    def test_missing_console_session_fails_and_names_the_fix(self):
        # Automatic login already on: a restart creates the session.
        self.set_pref('com.apple.loginwindow', 'autoLoginUser', 'fixture')
        result = self.shell('check_console_session; print_checks')
        self.assertNotEqual(result.returncode, 0)
        self.assertRegex(result.stderr, r'Console session\s+FAIL\s+none; restart this Mac')
        # Automatic login off (or for another user): turn it on first.
        for user in (None, 'someone-else'):
            with self.subTest(user=user):
                path = self.root / 'defaults/com.apple.loginwindow/autoLoginUser'
                path.unlink(missing_ok=True)
                if user:
                    self.set_pref('com.apple.loginwindow', 'autoLoginUser', user)
                result = self.shell('check_console_session; print_checks')
                self.assertNotEqual(result.returncode, 0)
                self.assertRegex(result.stderr, r'Console session\s+FAIL\s+none; turn on automatic login')

    def test_console_session_is_skipped_without_auto_login(self):
        result = self.shell('AUTO_LOGIN=0; check_console_session; print_checks')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stderr, r'Console session\s+skip\s+none \(--skip-auto-login\)')

    def test_verify_host_reports_the_console_session(self):
        stubs = self.HOST_STUBS.replace('check_console_session() { :; }\n', '')
        (self.root / 'gui-session').touch()
        result = self.shell(stubs + 'check_unattended_preferences() { :; }\nverify_host; print_checks')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stderr, r'Console session\s+ok')

    def test_runner_options_are_rejected(self):
        result = self.shell('parse_args --org acme')
        self.assertEqual(result.returncode, 2)
        self.assertIn('Unknown argument: --org', result.stderr)

    def test_auto_login_is_on_by_default(self):
        result = self.shell('parse_args; echo "auto=$AUTO_LOGIN"')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('auto=1', result.stdout)

    def test_skip_auto_login_never_calls_sysadminctl(self):
        result = self.shell('parse_args --skip-auto-login; configure_auto_login')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.lines('sysadminctl-calls'), [])

    def test_pending_decryption_is_a_warning_not_a_failure(self):
        (self.root/'filevault').write_text('FileVault is On.\nDecryption in progress\n')
        result = self.shell('check_auto_login; print_checks')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stderr, r'Automatic login\s+warn')

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

    def test_clear_desktop_is_left_alone(self):
        self.clear_desktop()
        result = self.shell('configure_desktop')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('already clear', result.stderr)
        self.assertFalse(any(call.startswith(('write ', 'export '))
                             for call in self.lines('defaults-calls')))

    def test_recent_apps_still_shown_is_not_clear(self):
        self.clear_desktop()
        self.set_pref('com.apple.dock', 'show-recents', '1')
        self.assertNotEqual(self.shell('desktop_is_clear').returncode, 0)

    def test_skip_clear_desktop_leaves_preferences_alone(self):
        result = self.shell('parse_args --skip-clear-desktop; configure_desktop')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.lines('defaults-calls'), [])

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
        result = self.shell('AUTO_LOGIN=0; check_filevault; check_auto_login; check_desktop; print_checks')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('This Mac is ready', result.stderr)

    def test_default_auto_login_that_is_off_fails_the_check(self):
        (self.root/'filevault').write_text('FileVault is Off.\n')
        result = self.shell('check_auto_login; print_checks')
        self.assertNotEqual(result.returncode, 0)
        self.assertRegex(result.stderr, r'Automatic login\s+FAIL')


if __name__ == '__main__':
    unittest.main()
