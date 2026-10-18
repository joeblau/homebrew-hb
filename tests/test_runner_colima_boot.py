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
BIN = REPO / "bin"
BUILDER = BIN / 'runner-docker-builder'


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
            self.assertIs(config['KeepAlive'], True)
            self.assertTrue(config['SessionCreate'])
            self.assertEqual(config['UserName'],'fixture')
            self.assertEqual(config['WorkingDirectory'],str(self.runner_root/f'runner-{index}'))
            self.assertEqual(config['EnvironmentVariables']['HOME'],str(self.root/'home'))
            self.assertNotIn('LimitLoadToSessionType',config)
            self.assertEqual(config['EnvironmentVariables']['DOCKER_CONFIG'],str(self.root/'docker-config'))
            self.assertEqual(config['EnvironmentVariables']['COLIMA_HOME'],str(self.root/'colima-home'))
            if index == 1:
                self.assertEqual(args[5:], [str(self.runner_root/'runner-1/bin/runsvc.sh')])
            else:
                self.assertEqual(args[5], '/fixture/runner-ephemeral')
                self.assertIn('--org',args)
        calls=(self.root/'sudo-calls').read_text().splitlines()
        for index in (1,2):
            plist=self.plists/f'com.github.runner-{index}.plist'
            bootstrap=calls.index(f'launchctl bootstrap system {plist}')
            self.assertLess(calls.index(f'chown root:wheel {plist}'),bootstrap)
            self.assertLess(calls.index(f'chmod 644 {plist}'),bootstrap)
            self.assertLess(calls.index(f'launchctl enable system/com.github.runner-{index}'),bootstrap)
        self.assertFalse(any('kickstart -k' in call for call in calls))

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
UPGRADE_BIN="$TEST_ROOT/upgrade"
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

    def test_retrofit_resumes_runner_left_in_maintenance(self):
        self.existing_runner()
        hold = self.runner_root/'.maintenance'
        hold.mkdir()
        (hold/'runner-1').touch()
        result = self.shell(self.CONFIG + 'UPGRADE_BIN="$TEST_ROOT/upgrade"; configure_runner 1')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertFalse((hold/'runner-1').exists())
        self.assertTrue((self.root/'upgrade-calls').read_text().splitlines()[-1].startswith('start --runner 1'))

    def test_unchanged_held_runner_is_started_again(self):
        self.existing_runner()
        first = self.shell(self.CONFIG + 'UPGRADE_BIN="$TEST_ROOT/upgrade"; configure_runner 1')
        self.assertEqual(first.returncode,0,first.stderr)
        (self.runner_root/'.maintenance/runner-1').touch()
        result = self.shell(self.CONFIG + 'UPGRADE_BIN="$TEST_ROOT/upgrade"; configure_runner 1')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertFalse((self.runner_root/'.maintenance/runner-1').exists())
        self.assertEqual((self.root/'upgrade-calls').read_text().splitlines()[-1].split()[:3],
                         ['start','--runner','1'])

    def test_loaded_runner_without_colima_is_migrated_without_registering_again(self):
        directory = self.existing_runner()
        path = self.plists/'com.github.runner-1.plist'
        config = self.read_plist()
        config.update({'RunAtLoad':False, 'KeepAlive':{'SuccessfulExit':False},
                       'SessionCreate':False, 'LimitLoadToSessionType':'Aqua', 'Disabled':True})
        config['EnvironmentVariables']['HOME'] = '/wrong/login/home'
        path.write_bytes(plistlib.dumps(config))
        (self.root/'loaded-com.github.runner-1').touch()
        (directory/'_work').mkdir()
        (directory/'_work/keep').write_text('workspace')
        (directory/'config.sh').write_text('#!/bin/bash\nexit 99\n')
        result = self.shell('''
COLIMA=0; UPGRADE_BIN="$TEST_ROOT/upgrade"
configure_runner 1 || exit 91
configure_runner 1 || exit 92
''')
        self.assertEqual(result.returncode,0,result.stderr)
        config = self.read_plist()
        self.assertIs(config['KeepAlive'],True)  # restart even after a clean exit
        self.assertIs(config['RunAtLoad'],True)
        self.assertIs(config['SessionCreate'],True)
        self.assertNotIn('LimitLoadToSessionType',config)
        self.assertNotIn('Disabled',config)
        self.assertEqual(config['ProgramArguments'],[str(directory/'bin/runsvc.sh')])
        env = config['EnvironmentVariables']
        self.assertEqual(env['HOME'],str(self.root/'home'))
        self.assertEqual(env['USER'],'fixture')
        self.assertEqual(env['LOGNAME'],'fixture')
        self.assertEqual(env['OPERATOR'],'keep & me')
        self.assertEqual((directory/'.credentials').read_text(),'keep credentials')
        self.assertEqual((directory/'_work/keep').read_text(),'workspace')
        self.assertEqual(len((self.root/'upgrade-calls').read_text().splitlines()),2)

    def test_existing_ephemeral_supervisor_keeps_arguments_during_migration(self):
        self.existing_runner()
        config = self.read_plist()
        args = ['/fixture/runner-ephemeral','--org','acme','--labels','special,macmini',
                '--ram-scratch','--ram-scratch-mb','512']
        config['ProgramArguments'] = args
        (self.plists/'com.github.runner-1.plist').write_bytes(plistlib.dumps(config))
        result = self.shell(self.CONFIG + 'UPGRADE_BIN="$TEST_ROOT/upgrade"; configure_runner 1')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(self.read_plist()['ProgramArguments'][5:],args)

    GUI_FIXTURE = r'''
eval "$(declare -f sudo | sed '1s/sudo/sudo_fixture/')"
sudo() {
  if [[ "$1 $2" == 'launchctl asuser' ]]; then
    case "$5" in
      print) [[ -f "$TEST_ROOT/gui-loaded" ]] ;;
      bootout) rm -f "$TEST_ROOT/gui-loaded" ;;
      *) return 1 ;;
    esac
  else
    sudo_fixture "$@"
  fi
}
'''

    def prepare_gui_fixture(self):
        directory = self.existing_runner()
        result = self.shell(self.CONFIG + 'UPGRADE_BIN="$TEST_ROOT/upgrade"; configure_runner 1')
        self.assertEqual(result.returncode,0,result.stderr)
        agent = self.root/'home/Library/LaunchAgents/actions.runner.acme.ci-runner-1.plist'
        agent.parent.mkdir(parents=True)
        agent.write_text('login-only service')
        (self.root/'gui-loaded').touch()
        return directory, agent

    def test_login_agent_is_removed_even_when_system_configuration_is_unchanged(self):
        _, agent = self.prepare_gui_fixture()
        result = self.shell(self.CONFIG + self.GUI_FIXTURE + '''
UPGRADE_BIN="$TEST_ROOT/upgrade"
ps() { echo /sbin/launchd; }
configure_runner 1
''')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertFalse(agent.exists())
        self.assertFalse((self.root/'gui-loaded').exists())
        self.assertEqual(len((self.root/'upgrade-calls').read_text().splitlines()),2)

    def test_active_login_agent_job_prevents_removal(self):
        directory, agent = self.prepare_gui_fixture()
        result = self.shell(self.CONFIG + self.GUI_FIXTURE + '''
UPGRADE_BIN="$TEST_ROOT/upgrade"
ps() { echo "$TEST_ROOT/runners/runner-1/bin/Runner.Worker"; }
configure_runner 1
''')
        self.assertNotEqual(result.returncode,0)
        self.assertIn('Active job',result.stderr)
        self.assertTrue(agent.exists())
        self.assertTrue((self.root/'gui-loaded').exists())
        self.assertEqual((directory/'.credentials').read_text(),'keep credentials')

    def test_unknown_runner_wrapper_is_not_replaced(self):
        self.existing_runner()
        path=self.plists/'com.github.runner-1.plist'
        config=self.read_plist(); config['ProgramArguments']=['/operator/custom-wrapper']
        path.write_bytes(plistlib.dumps(config)); before=path.read_bytes()
        result=self.shell(self.CONFIG+'UPGRADE_BIN="$TEST_ROOT/upgrade"; configure_runner 1')
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
        self.assertTrue(config['RunAtLoad'])
        self.assertTrue(config['SessionCreate'])
        self.assertNotIn('LimitLoadToSessionType',config)
        calls=(self.root/'sudo-calls').read_text().splitlines()
        plist=self.plists/'com.github.runner-colima.plist'
        bootstrap=calls.index(f'launchctl bootstrap system {plist}')
        self.assertLess(calls.index(f'chown root:wheel {plist}'),bootstrap)
        self.assertLess(calls.index(f'chmod 644 {plist}'),bootstrap)
        args = config['ProgramArguments']
        self.assertEqual(Path(args[0]).name, 'runner-docker-builder')
        self.assertEqual(args[1:], ['boot-colima'])
        self.assertTrue(config['RunAtLoad']);self.assertTrue(config['KeepAlive'])
        self.assertEqual(config['EnvironmentVariables']['HOME'],str(self.root/'home'))
        self.assertEqual((self.root/'colima-setup').read_text(),'setup\n')
        self.assertEqual(config['ExitTimeOut'],120)

    COLIMA_SETUP_FIXTURE = r'''
export PATH="$TEST_ROOT/bin:$PATH"
as_user() {
  if [[ "$*" == *runner-docker-builder*setup-colima* ]]; then
    echo provision >> "$TEST_ROOT/vm-events"
    touch "$TEST_ROOT/login-session-vm"
    return 0
  fi
  "$@"
}
ps() { echo /sbin/launchd; }
eval "$(declare -f sudo | sed '1s/sudo/sudo_fixture/')"
sudo() {
  if [[ "$1 $2" == 'launchctl bootstrap' ]]; then
    [[ ! -f "$TEST_ROOT/login-session-vm" ]] || return 98
    echo bootstrap >> "$TEST_ROOT/vm-events"
  fi
  sudo_fixture "$@"
}
'''

    def colima_binary(self, code):
        bin_dir = self.root/'bin'
        bin_dir.mkdir()
        binary = bin_dir/'colima'
        binary.write_text('#!/bin/bash\n' + code + '\n')
        binary.chmod(0o755)

    def test_colima_vm_is_stopped_before_handoff_to_system_daemon(self):
        self.colima_binary('''
[[ "$*" == 'stop default' ]] || exit 99
echo stop >> "$TEST_ROOT/vm-events"
rm -f "$TEST_ROOT/login-session-vm"
''')
        result = self.shell(self.COLIMA_SETUP_FIXTURE + 'prepare_colima_boot')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual((self.root/'vm-events').read_text().splitlines(),['provision','stop','bootstrap'])
        self.assertTrue((self.root/'loaded-com.github.runner-colima').exists())

    def test_colima_handoff_stop_failure_never_bootstraps_a_supervisor(self):
        self.colima_binary('exit 42')
        result = self.shell(self.COLIMA_SETUP_FIXTURE + 'prepare_colima_boot')
        self.assertNotEqual(result.returncode,0)
        self.assertEqual((self.root/'vm-events').read_text().splitlines(),['provision'])
        self.assertFalse((self.root/'loaded-com.github.runner-colima').exists())

    def test_colima_stop_timeout_preserves_existing_service(self):
        self.colima_binary('echo unexpected >> "$TEST_ROOT/vm-events"')
        path = self.plists/'com.github.runner-colima.plist'
        original = plistlib.dumps({'UserName':'fixture'})
        path.write_bytes(original)
        result = self.shell(self.COLIMA_SETUP_FIXTURE + '''
sleep() { :; }
sudo() {
  if [[ "$1 $2" == 'launchctl print' ]]; then return 0; fi
  if [[ "$1 $2" == 'launchctl bootout' ]]; then return 0; fi
  sudo_fixture "$@"
}
prepare_colima_boot
''')
        self.assertNotEqual(result.returncode,0)
        self.assertIn('Timed out stopping',result.stderr)
        self.assertEqual(path.read_bytes(),original)
        self.assertEqual((self.root/'vm-events').read_text().splitlines(),['provision'])

    def test_later_setup_inherits_colima_for_autoscaled_runners(self):
        (self.plists/'com.github.runner-colima.plist').write_text('fixture')
        result = self.shell('''
export TMPDIR="$TEST_ROOT"
detect_platform() { :; }; require_cmds() { :; }; detect_user() { :; }
detect_arch() { :; }; detect_host() { :; }; ensure_sudo() { :; }
configure_power() { :; }  # host power configuration is isolated in test_runner_power
configure_filevault() { :; }; ensure_host_rosetta() { :; }; self_upgrade() { :; }
configure_remote_access() { :; }; configure_tailscale() { :; }
start_runners() { echo "started $RUNNERS"; }
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
        self.assertIn('started 3', result.stdout)

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


class BootDaemonMigrationTests(unittest.TestCase):
    setUp = setup_fixture.SetupToolCacheTests.setUp
    HARNESS = setup_fixture.SetupToolCacheTests.HARNESS
    shell = setup_fixture.SetupToolCacheTests.shell

    def write_daemon(self, args):
        config = {'Label': 'com.github.runner-colima', 'UserName': 'fixture',
                  'RunAtLoad': True, 'KeepAlive': True, 'SessionCreate': True,
                  'WorkingDirectory': str(self.root/'home'),
                  'EnvironmentVariables': {'HOME': str(self.root/'home'), 'OPERATOR': 'keep',
                                           'COLIMA_HOME': str(self.root/'colima-home'),
                                           'DOCKER_CONFIG': str(self.root/'docker-config'),
                                           'RUNNER_DOCKER_TIMEOUT': '120'},
                  'ProgramArguments': args}
        path = self.plists/'com.github.runner-colima.plist'
        path.write_bytes(plistlib.dumps(config))
        return path

    BODY = """
COLIMA=1; COLIMA_INHERITED=1
ps() { cat "$TEST_ROOT/ps" 2>/dev/null || echo /sbin/launchd; }
restart_colima_without_rosetta() { echo probed >> "$TEST_ROOT/rosetta-probes"; }
prepare_colima_boot
"""

    def test_direct_colima_daemon_is_migrated_to_the_boot_supervisor(self):
        path = self.write_daemon(['/opt/homebrew/bin/colima', 'start', 'default', '--foreground'])
        (self.root/'loaded-com.github.runner-colima').touch()
        result = self.shell(self.BODY)
        self.assertEqual(result.returncode, 0, result.stderr)
        config = plistlib.loads(path.read_bytes())
        args = config['ProgramArguments']
        self.assertEqual(Path(args[0]).name, 'runner-docker-builder')
        self.assertEqual(args[1:], ['boot-colima'])
        self.assertEqual(config['EnvironmentVariables']['OPERATOR'], 'keep')
        calls = (self.root/'sudo-calls').read_text().splitlines()
        self.assertIn('launchctl bootout system/com.github.runner-colima', calls)
        self.assertTrue((self.root/'loaded-com.github.runner-colima').exists())
        # The freshly restarted supervisor verifies Rosetta itself.
        self.assertFalse((self.root/'rosetta-probes').exists())

    def test_supervisor_daemon_migration_is_idempotent(self):
        self.write_daemon(['/opt/homebrew/bin/colima', 'start', 'default', '--foreground'])
        (self.root/'loaded-com.github.runner-colima').touch()
        first = self.shell(self.BODY)
        self.assertEqual(first.returncode, 0, first.stderr)
        (self.root/'sudo-calls').unlink()
        second = self.shell(self.BODY)
        self.assertEqual(second.returncode, 0, second.stderr)
        calls = (self.root/'sudo-calls').read_text().splitlines()
        self.assertNotIn('launchctl bootout system/com.github.runner-colima', calls)
        self.assertEqual((self.root/'rosetta-probes').read_text(), 'probed\n')

    def test_active_job_blocks_daemon_migration(self):
        path = self.write_daemon(['/opt/homebrew/bin/colima', 'start', 'default', '--foreground'])
        before = path.read_bytes()
        (self.root/'loaded-com.github.runner-colima').touch()
        (self.root/'ps').write_text('/opt/github-runners/runner-1/bin/Runner.Worker\n')
        result = self.shell(self.BODY)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Active runner job', result.stderr)
        self.assertEqual(path.read_bytes(), before)


class BootSupervisorTests(unittest.TestCase):
    """Drive `runner-docker-builder boot-colima` against a fake colima and arch."""

    COLIMA = """
import json, os, pathlib, signal, sys, time
root = pathlib.Path(os.environ['TEST_ROOT'])
args = sys.argv[1:]
with (root/'colima-calls.jsonl').open('a') as handle:
    handle.write(json.dumps(args) + chr(10))
if args and args[0] == 'start':
    running = root/'vm-running'
    running.write_text(str(os.getpid()))
    def stop(signum, frame):
        running.unlink()
        (root/'vm-stopped').write_text('stopped')
        sys.exit(143)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    for _ in range(2400):
        time.sleep(0.05)
    sys.exit(0)
if args and args[0] == 'ssh':
    if not (root/'vm-running').exists():
        sys.exit(1)
    state = root/'vm-rosetta'
    print(state.read_text().strip() if state.exists() else 'missing')
sys.exit(0)
"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        bin_dir = self.root/'bin'
        bin_dir.mkdir()
        (self.root/'home').mkdir()
        colima = bin_dir/'colima'
        colima.write_text(f'#!{sys.executable}\n' + self.COLIMA)
        colima.chmod(0o755)
        arch = bin_dir/'arch'
        arch.write_text('#!/bin/bash\n[[ -f "$TEST_ROOT/host-rosetta" ]]\n')
        arch.chmod(0o755)

    def boot(self, **env):
        self.log = open(self.root/'boot.log', 'w')
        self.addCleanup(self.log.close)
        process = subprocess.Popen(
            [str(BUILDER), 'boot-colima'],
            env=dict(os.environ, TEST_ROOT=str(self.root), HOME=str(self.root/'home'),
                     PATH=f'{self.root/"bin"}:{os.environ["PATH"]}',
                     RUNNER_BOOT_ROSETTA_WAIT='1', RUNNER_BOOT_VERIFY_WAIT='6',
                     RUNNER_BOOT_RECHECK='0.1', **env),
            text=True, stderr=self.log)
        self.addCleanup(self.reap, process)
        return process

    def reap(self, process):
        if process.poll() is None:
            process.kill()
            process.wait()

    def wait_for(self, condition, timeout=15):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if condition():
                return True
            time.sleep(0.05)
        return False

    def boot_log(self):
        return (self.root/'boot.log').read_text()

    def ssh_calls(self):
        path = self.root/'colima-calls.jsonl'
        if not path.exists():
            return []
        return [call for call in map(json.loads, path.read_text().splitlines())
                if call[:1] == ['ssh']]

    def marker(self):
        return self.root/'home/Library/Logs/runner-colima/rosetta-restarts'

    def test_vm_with_rosetta_keeps_running_and_clears_the_restart_counter(self):
        (self.root/'host-rosetta').touch()
        (self.root/'vm-rosetta').write_text('enabled')
        self.marker().parent.mkdir(parents=True)
        self.marker().write_text('2')
        process = self.boot()
        self.assertTrue(self.wait_for(lambda: not self.marker().exists()), self.boot_log())
        self.assertIsNone(process.poll())
        process.terminate()
        process.wait(timeout=15)
        self.assertTrue(self.wait_for(lambda: (self.root/'vm-stopped').exists()))

    def test_vm_without_rosetta_is_stopped_and_the_daemon_exits_for_a_retry(self):
        (self.root/'host-rosetta').touch()
        process = self.boot()
        self.assertEqual(process.wait(timeout=30), 1, self.boot_log())
        self.assertTrue((self.root/'vm-stopped').exists())
        self.assertEqual(self.marker().read_text(), '1')
        self.assertIn('restarting the VM (attempt 1 of 3)', self.boot_log())

    def test_restart_cap_leaves_the_vm_running_after_three_attempts(self):
        (self.root/'host-rosetta').touch()
        self.marker().parent.mkdir(parents=True)
        self.marker().write_text('3')
        process = self.boot()
        self.assertTrue(self.wait_for(
            lambda: 'still lacks Rosetta after 3 restarts' in self.boot_log()))
        self.assertIsNone(process.poll())
        self.assertTrue((self.root/'vm-running').exists())
        process.terminate()
        process.wait(timeout=15)

    def test_qemu_opt_out_starts_the_vm_without_any_rosetta_checks(self):
        process = self.boot(RUNNER_ALLOW_QEMU='1')
        self.assertTrue(self.wait_for(lambda: (self.root/'vm-running').exists()))
        time.sleep(0.5)
        self.assertIsNone(process.poll())
        self.assertEqual(self.ssh_calls(), [])
        process.terminate()
        process.wait(timeout=15)

    def test_missing_host_rosetta_still_starts_the_gated_vm_after_the_wait(self):
        process = self.boot()
        self.assertTrue(self.wait_for(lambda: (self.root/'vm-running').exists()))
        self.assertIn('Host Rosetta is not available', self.boot_log())
        self.assertIsNone(process.poll())
        self.assertEqual(self.ssh_calls(), [])
        process.terminate()
        process.wait(timeout=15)


if __name__ == '__main__':
    unittest.main()
