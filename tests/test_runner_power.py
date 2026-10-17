"""Host power policy tests using fake pmset/sudo; never alter real power settings."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from test_runner_toolcache import definitions


MOCK = r'''
import json, os, pathlib, sys
root = pathlib.Path(os.environ['TEST_ROOT'])
name, args = pathlib.Path(sys.argv[0]).name, sys.argv[1:]
with (root/'calls.jsonl').open('a') as handle:
    handle.write(json.dumps([name] + args) + '\n')
state = json.loads((root/'state.json').read_text())
if name == 'sudo':
    assert args[:2] == ['pmset', '-a'], args
    os.execv(str(root/'bin/pmset'), args)
if args == ['-g', 'cap']:
    if state.get('cap_rc'):
        sys.exit(state['cap_rc'])
    print('Capabilities for AC Power:')
    for key in state['capabilities']:
        print(' ' + key)
elif args == ['-g', 'custom']:
    if state.get('readback_rc'):
        sys.exit(state['readback_rc'])
    for profile, values in state['profiles'].items():
        print(profile + ':')
        for key, value in values.items():
            print(' ' + key + ' ' + str(value))
elif args[:1] == ['-a']:
    if state.get('write_rc'):
        sys.exit(state['write_rc'])
    for key, value in zip(args[1::2], args[2::2]):
        assert key in state['capabilities'], key
        for profile, values in state['profiles'].items():
            if profile != state.get('ignore_profile') and key != state.get('ignore_key'):
                values[key] = int(value)
    (root/'state.json').write_text(json.dumps(state))
else:
    sys.exit(99)
'''


class PowerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root/'bin').mkdir()
        for name in ('pmset', 'sudo'):
            command = self.root/'bin'/name
            command.write_text(f'#!{sys.executable}\n' + MOCK)
            command.chmod(0o755)
        self.state = {
            'capabilities': ['autorestart', 'sleep', 'disksleep', 'powernap'],
            'profiles': {
                'AC Power': {'autorestart': 0, 'sleep': 30, 'disksleep': 10, 'powernap': 1,
                             'displaysleep': 7, 'womp': 1},
                'UPS Power': {'autorestart': 0, 'sleep': 30, 'disksleep': 10, 'powernap': 1},
            },
        }

    def shell(self, body='configure_power'):
        (self.root/'state.json').write_text(json.dumps(self.state))
        return subprocess.run(
            ['/bin/bash', '-c', definitions('runner-setup') + '\ntrap - EXIT\n' + body],
            env={'PATH': f'{self.root}/bin:/usr/bin:/bin', 'TEST_ROOT': str(self.root)},
            capture_output=True, text=True, timeout=10,
        )

    def calls(self):
        path = self.root/'calls.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_default_policy_applies_to_every_profile_and_preserves_other_settings(self):
        result = self.shell()
        self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads((self.root/'state.json').read_text())
        for profile in state['profiles'].values():
            self.assertEqual({key: profile[key] for key in self.state['capabilities']},
                             {'autorestart': 1, 'sleep': 0, 'disksleep': 0, 'powernap': 0})
        self.assertEqual(state['profiles']['AC Power']['displaysleep'], 7)
        self.assertEqual(state['profiles']['AC Power']['womp'], 1)
        self.assertEqual(len([call for call in self.calls() if call[0] == 'sudo']), 1)

    def test_wake_and_keepalive_are_enabled_where_supported(self):
        self.state['capabilities'] += ['womp', 'tcpkeepalive']
        for profile in self.state['profiles'].values():
            profile.update({'womp': 0, 'tcpkeepalive': 0})
        result = self.shell()
        self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads((self.root/'state.json').read_text())
        for profile in state['profiles'].values():
            self.assertEqual(profile['womp'], 1)
            self.assertEqual(profile['tcpkeepalive'], 1)

    def test_unsupported_settings_are_skipped(self):
        self.state['capabilities'] = ['sleep', 'disksleep']
        result = self.shell()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Skipping autorestart: not supported', result.stderr)
        self.assertIn('Skipping powernap: not supported', result.stderr)
        write = next(call for call in self.calls() if call[:2] == ['pmset', '-a'])
        self.assertNotIn('powernap', write)
        self.assertNotIn('autorestart', write)

    def test_skip_option_does_not_even_inspect_power_settings(self):
        result = self.shell('parse_args --org acme --token dummy --skip-power-config\nconfigure_power')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), [])

    def test_no_supported_settings_needs_no_privileged_changes(self):
        self.state['capabilities'] = []
        result = self.shell()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any(call[0] == 'sudo' for call in self.calls()))

    def test_capability_read_failure_makes_no_changes(self):
        self.state['cap_rc'] = 1
        result = self.shell()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Could not read supported', result.stderr)
        self.assertFalse(any(call[0] == 'sudo' for call in self.calls()))

    def test_write_failure_stops_setup(self):
        self.state['write_rc'] = 1
        result = self.shell()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Could not apply host power settings', result.stderr)

    def test_readback_failure_is_reported(self):
        self.state['readback_rc'] = 1
        result = self.shell()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Could not verify host power settings', result.stderr)

    def test_mismatch_in_one_profile_is_not_reported_as_success(self):
        self.state['ignore_profile'] = 'UPS Power'
        result = self.shell()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('autorestart=1 could not be verified', result.stderr)

    def test_missing_readback_key_is_not_reported_as_success(self):
        self.state['ignore_key'] = 'autorestart'
        for profile in self.state['profiles'].values():
            del profile['autorestart']
        result = self.shell()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('autorestart=1 could not be verified', result.stderr)

    def test_power_failure_aborts_main_before_runner_or_colima_provisioning(self):
        self.state['write_rc'] = 1
        result = self.shell('''
RUNNER_ROOT="$TEST_ROOT/runners"; LAUNCH_DAEMON_DIR="$TEST_ROOT/plists"
detect_platform() { :; }; require_cmds() { :; }; detect_user() { :; }
detect_arch() { :; }; detect_host() { :; }; ensure_sudo() { :; }
prepare_runner_archive() { touch "$TEST_ROOT/provisioned"; }
prepare_root_dir() { touch "$TEST_ROOT/provisioned"; }
prepare_colima_boot() { touch "$TEST_ROOT/provisioned"; }
configure_runner() { touch "$TEST_ROOT/provisioned"; }
main --org acme --token dummy --colima
''')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root/'provisioned').exists())


if __name__ == '__main__':
    unittest.main()
