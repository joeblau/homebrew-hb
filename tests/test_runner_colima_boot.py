"""Colima boot supervision and pre-listener readiness, without VM/launchd writes."""
import json
import os
from pathlib import Path
import plistlib
import signal
import subprocess
import sys
import tempfile
import time
import unittest

import test_runner_toolcache as setup_fixture

REPO = Path(__file__).resolve().parents[1]
BUILDER = REPO / 'runner-docker-builder'


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        docker = self.root / 'docker'
        docker.write_text(f'#!{sys.executable}\n' + '''
import json, os, pathlib, subprocess, sys, time
root = pathlib.Path(os.environ['TEST_ROOT'])
count = int((root/'count').read_text()) + 1 if (root/'count').exists() else 1
(root/'count').write_text(str(count))
(root/'probe.json').write_text(json.dumps({'args':sys.argv[1:], 'context':os.environ.get('DOCKER_CONTEXT'), 'host':os.environ.get('DOCKER_HOST')}))
if os.environ.get('HANG'):
    child = subprocess.Popen(['sleep', '60'])
    (root/'child').write_text(str(child.pid))
    time.sleep(60)
sys.exit(0 if count >= int(os.environ.get('READY_AT','1')) else 1)
''')
        docker.chmod(0o755)
        # Never reach a real VM: the gate strips QEMU handlers through colima ssh.
        colima = self.root / 'colima'
        colima.write_text(f'#!{sys.executable}\n' + '''
import json, os, pathlib, sys
root = pathlib.Path(os.environ['TEST_ROOT'])
with (root/'colima-calls').open('a') as f:
    f.write(json.dumps({'args':sys.argv[1:], 'profile':os.environ.get('COLIMA_PROFILE')}) + '\\n')
print(os.environ.get('COLIMA_STATE', 'rosetta'))
sys.exit(int(os.environ.get('COLIMA_RC', '0')))
''')
        colima.chmod(0o755)
        self.env = dict(os.environ, TEST_ROOT=str(self.root), PATH=f'{self.root}:' + os.environ['PATH'],
                        DOCKER_CONTEXT='desktop-linux', DOCKER_HOST='tcp://wrong.invalid:2375')
        self.env.pop('RUNNER_ALLOW_QEMU', None)
        self.command = [str(BUILDER), 'wait-colima', '--timeout', '5', '--', sys.executable, '-c',
                        'import os,pathlib; pathlib.Path(os.environ["TEST_ROOT"],"started").write_text(os.environ["DOCKER_CONTEXT"])']

    def test_delayed_docker_readiness_precedes_runner_and_overrides_wrong_context(self):
        result = subprocess.run(self.command, env=dict(self.env, READY_AT='2'), text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root/'started').read_text(), 'colima')
        self.assertEqual((self.root/'count').read_text(), '2')
        probe = json.loads((self.root/'probe.json').read_text())
        self.assertEqual(probe['args'], ['--context','colima','info'])
        self.assertIsNone(probe['host'])

    def test_timeout_never_starts_runner_and_kills_hung_probe_children(self):
        command = list(self.command)
        command[3] = '1'
        start = time.monotonic()
        result = subprocess.run(command, env=dict(self.env, HANG='1'), text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertLess(time.monotonic() - start, 4)
        self.assertFalse((self.root/'started').exists())
        pid = (self.root/'child').read_text()
        probe = subprocess.run(['ps','-p',pid,'-o','stat='],capture_output=True,text=True)
        self.assertTrue(probe.returncode != 0 or probe.stdout.strip().startswith('Z'), probe.stdout)

    def test_stop_while_waiting_does_not_start_runner(self):
        process = subprocess.Popen(self.command, env=dict(self.env, HANG='1'), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: process.kill() if process.poll() is None else None)
        deadline = time.monotonic() + 5
        while not (self.root/'child').exists() and time.monotonic() < deadline:
            time.sleep(.02)
        process.terminate()
        _, stderr = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 143, stderr)
        self.assertFalse((self.root/'started').exists())

    def test_gate_preserves_argv_and_service_exit_status(self):
        command = self.command[:5] + [sys.executable, '-c',
                    'import sys; assert sys.argv[1:] == ["space value", "$(literal)"]; sys.exit(7)',
                    'space value', '$(literal)']
        result = subprocess.run(command, env=self.env, text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 7, result.stderr)

    def colima_calls(self):
        path = self.root/'colima-calls'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    @unittest.skipUnless(os.uname().machine == 'arm64', 'Rosetta gate applies to Apple Silicon only')
    def test_gate_strips_qemu_and_requires_rosetta_before_starting(self):
        result = subprocess.run(self.command, env=self.env, text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.root/'started').exists())
        [call] = self.colima_calls()
        self.assertEqual(call['args'][:5], ['ssh','--','sudo','sh','-c'])
        self.assertIn('qemu-x86_64', call['args'][5])
        self.assertIn('rosetta', call['args'][5])
        self.assertEqual(call['profile'], 'default')

    @unittest.skipUnless(os.uname().machine == 'arm64', 'Rosetta gate applies to Apple Silicon only')
    def test_missing_rosetta_or_failed_removal_keeps_runner_offline(self):
        for extra in ({'COLIMA_STATE':'none'}, {'COLIMA_RC':'3'}):
            result = subprocess.run(self.command, env=dict(self.env, **extra), text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn('runner remains offline', result.stderr)
            self.assertFalse((self.root/'started').exists())

    def test_allow_qemu_opt_out_skips_vm_changes(self):
        result = subprocess.run(self.command, env=dict(self.env, RUNNER_ALLOW_QEMU='1', COLIMA_RC='3'),
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.colima_calls(), [])

    def test_invalid_timeout_and_missing_service_rejected(self):
        for args in (['--timeout','0','--','true'], ['--timeout','no','--','true'], ['--']):
            result = subprocess.run([str(BUILDER),'wait-colima',*args], env=self.env, capture_output=True)
            self.assertEqual(result.returncode, 2)
        self.assertFalse((self.root/'count').exists())


class BootProvisioningTests(unittest.TestCase):
    setUp = setup_fixture.SetupToolCacheTests.setUp
    make_runner = setup_fixture.SetupToolCacheTests.make_runner
    HARNESS = setup_fixture.SetupToolCacheTests.HARNESS
    shell = setup_fixture.SetupToolCacheTests.shell

    CONFIG = '''
COLIMA=1; DOCKER_HELPER=/fixture/runner-docker-builder
COLIMA_DOCKER_CONFIG="$TEST_ROOT/docker-config"; DOCKER_TIMEOUT=120
COLIMA_CONFIG_HOME="$TEST_ROOT/colima-home"
'''

    def read_plist(self, name='com.github.runner-1'):
        return plistlib.loads((self.plists / (name + '.plist')).read_bytes())

    def test_persistent_and_ephemeral_plists_wait_before_starting(self):
        for index in (1,2):
            self.make_runner(index)
        result = self.shell(self.CONFIG + '''
configure_runner 1 || exit 91
EPHEMERAL=1; EPHEMERAL_BIN=/fixture/runner-ephemeral; ORG=acme; GITHUB_PAT=dummy
configure_runner 2 || exit 92
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        for index in (1,2):
            config = self.read_plist(f'com.github.runner-{index}')
            args = config['ProgramArguments']
            self.assertEqual(args[:5], ['/fixture/runner-docker-builder','wait-colima','--timeout','120','--'])
            self.assertTrue(config['RunAtLoad'])
            self.assertEqual(config['KeepAlive'], {'SuccessfulExit':False})
            self.assertEqual(config['UserName'],'fixture')
            self.assertEqual(config['EnvironmentVariables']['DOCKER_CONFIG'],str(self.root/'docker-config'))
            self.assertEqual(config['EnvironmentVariables']['COLIMA_HOME'],str(self.root/'colima-home'))
            if index == 1:
                self.assertEqual(args[5:], [str(self.runner_root/'runner-1/bin/runsvc.sh')])
            else:
                self.assertEqual(args[5], '/fixture/runner-ephemeral')
                self.assertIn('--org',args)

    def existing_runner(self):
        self.make_runner(1)
        directory = self.runner_root/'runner-1'
        settings = {'agentName':'ci-runner-1','gitHubUrl':'https://github.com/acme'}
        (directory/'.runner').write_text(json.dumps(settings))
        (directory/'.credentials').write_text('keep credentials')
        config = {'Label':'com.github.runner-1','UserName':'fixture','WorkingDirectory':str(directory),
                  'ProgramArguments':[str(directory/'bin/runsvc.sh')],
                  'EnvironmentVariables':{'HOME':str(self.root/'home'),'OPERATOR':'keep & me'}}
        (self.plists/'com.github.runner-1.plist').write_bytes(plistlib.dumps(config))
        helper = self.root/'upgrade'
        helper.write_text('''#!/bin/bash
printf '%s\\n' "$*" >> "$TEST_ROOT/upgrade-calls"
mkdir -p "$TEST_ROOT/runners/.maintenance"
if [[ $1 == shutdown ]]; then touch "$TEST_ROOT/runners/.maintenance/runner-1"; fi
if [[ $1 == start ]]; then rm -f "$TEST_ROOT/runners/.maintenance/runner-1"; touch "$TEST_ROOT/loaded-com.github.runner-1"; fi
''')
        helper.chmod(0o755)
        return directory

    def test_retrofit_preserves_registration_and_is_idempotent(self):
        directory = self.existing_runner()
        before = (directory/'.runner').read_bytes()
        result = self.shell(self.CONFIG + '''
COLIMA_UPGRADE_BIN="$TEST_ROOT/upgrade"
configure_runner 1 || exit 91
configure_runner 1 || exit 92
''')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual((directory/'.runner').read_bytes(), before)
        self.assertEqual((directory/'.credentials').read_text(),'keep credentials')
        calls = (self.root/'upgrade-calls').read_text().splitlines()
        self.assertEqual(len(calls),2)
        self.assertTrue(calls[0].startswith('shutdown --runner 1'))
        self.assertTrue(calls[1].startswith('start --runner 1'))
        self.assertEqual(self.read_plist()['EnvironmentVariables']['OPERATOR'],'keep & me')

    def test_retrofit_preserves_existing_maintenance_hold(self):
        self.existing_runner()
        hold = self.runner_root/'.maintenance'
        hold.mkdir()
        (hold/'runner-1').touch()
        result = self.shell(self.CONFIG + 'COLIMA_UPGRADE_BIN="$TEST_ROOT/upgrade"; configure_runner 1')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertTrue((hold/'runner-1').exists())
        self.assertNotIn('start ', (self.root/'upgrade-calls').read_text())

    def test_unknown_runner_wrapper_is_not_replaced(self):
        self.existing_runner()
        path=self.plists/'com.github.runner-1.plist'
        config=self.read_plist(); config['ProgramArguments']=['/operator/custom-wrapper']
        path.write_bytes(plistlib.dumps(config)); before=path.read_bytes()
        result=self.shell(self.CONFIG+'COLIMA_UPGRADE_BIN="$TEST_ROOT/upgrade"; configure_runner 1')
        self.assertNotEqual(result.returncode,0)
        self.assertEqual(path.read_bytes(),before)
        self.assertFalse((self.root/'upgrade-calls').exists())

    def test_colima_retrofit_parsing_allows_no_token_but_normal_setup_requires_it(self):
        result=self.shell('TOKEN=; unset RUNNER_TOKEN; parse_args --org acme --colima')
        self.assertEqual(result.returncode,0,result.stderr)
        result=self.shell('TOKEN=; unset RUNNER_TOKEN; parse_args --org acme')
        self.assertEqual(result.returncode,2)

    def test_new_daemon_has_user_home_foreground_keepalive_and_one_shared_vm(self):
        bin_dir=self.root/'bin'; bin_dir.mkdir()
        colima=bin_dir/'colima';colima.write_text('#!/bin/bash\nexit 0\n');colima.chmod(0o755)
        result=self.shell('''
export PATH="$TEST_ROOT/bin:$PATH"
as_user() {
  if [[ "$*" == *runner-docker-builder*setup-colima* ]]; then
    echo setup >> "$TEST_ROOT/colima-setup"; return 0
  fi
  "$@"
}
ps() { echo /sbin/launchd; }
prepare_colima_boot || exit 91
COLIMA_INHERITED=1
prepare_colima_boot || exit 92
''')
        self.assertEqual(result.returncode,0,result.stderr)
        config=self.read_plist('com.github.runner-colima')
        self.assertEqual(config['UserName'],'fixture')
        self.assertEqual(config['ProgramArguments'],[str(colima),'start','default','--foreground'])
        self.assertTrue(config['RunAtLoad']);self.assertTrue(config['KeepAlive'])
        self.assertEqual(config['EnvironmentVariables']['HOME'],str(self.root/'home'))
        self.assertEqual((self.root/'colima-setup').read_text(),'setup\n')

    def test_later_setup_inherits_colima_for_autoscaled_runners(self):
        (self.plists/'com.github.runner-colima.plist').write_text('fixture')
        result = self.shell('''
export TMPDIR="$TEST_ROOT"
detect_platform() { :; }; require_cmds() { :; }; detect_user() { :; }
detect_arch() { :; }; detect_host() { :; }; ensure_sudo() { :; }
prepare_runner_archive() { :; }; prepare_root_dir() { :; }
prepare_colima_boot() {
  [[ "$COLIMA" == 1 && "$COLIMA_INHERITED" == 1 ]] || return 1
  echo prepared >> "$TEST_ROOT/prepared"
}
configure_runner() { [[ "$COLIMA" == 1 ]] && echo "gated $1"; }
main --org acme --token dummy --runners 3
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root/'prepared').read_text(), 'prepared\n')
        for index in range(1,4):
            self.assertIn(f'gated {index}', result.stdout)

    def test_tokenless_setup_rejects_missing_registration_before_mutations(self):
        result = self.shell('''
TOKEN=; unset RUNNER_TOKEN
detect_platform() { :; }; require_cmds() { :; }; detect_user() { :; }
detect_arch() { :; }; detect_host() { :; }
ensure_sudo() { echo unexpected > "$TEST_ROOT/mutated"; }
main --org acme --colima --runners 2
''')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('requires registration', result.stderr)
        self.assertFalse((self.root/'mutated').exists())

    def test_boot_setup_refuses_active_jobs_before_starting_colima(self):
        result=self.shell('ps() { echo /opt/github-runners/runner-1/bin/Runner.Worker; }; prepare_colima_boot')
        self.assertNotEqual(result.returncode,0)
        self.assertIn('Active runner job',result.stderr)
        self.assertFalse((self.plists/'com.github.runner-colima.plist').exists())


if __name__ == '__main__':
    unittest.main()
