"""Isolated lifecycle regression tests; no live GitHub or launchd calls."""
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()

    def shell(self, script, body, **env):
        # Evaluate the function definitions, excluding the sole CLI entrypoint.
        definitions = (REPO / script).read_text().rsplit('main "$@"', 1)[0]
        return subprocess.run(["/bin/bash", "-c", definitions + '\ntrap - EXIT\n' + body],
                              env=dict(os.environ, TEST_ROOT=str(self.root), **env),
                              text=True, capture_output=True)

    def test_existing_binaries_skip_release_network(self):
        for index in (1, 2):
            runner = self.root / f"runner-{index}"
            (runner / "bin").mkdir(parents=True)
            (runner / "config.sh").touch(mode=0o755)
            (runner / "bin/runsvc.sh").touch()
        result = self.shell("runner-setup", '''
RUNNER_ROOT="$TEST_ROOT"; RUNNERS=2
resolve_latest_release() { echo unexpected >&2; exit 77; }
download_tarball() { exit 78; }
prepare_runner_archive
''')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_scope_casing_does_not_restart_loaded_runner(self):
        runner = self.root / "runner-1"
        runner.mkdir()
        (runner / ".runner").write_text(json.dumps({"agentName": "host-runner-1", "gitHubUrl": "https://github.com/acme/repo/"}))
        (self.root / "com.github.runner-1.plist").touch()
        result = self.shell("runner-setup", '''
RUNNER_ROOT="$TEST_ROOT"; LAUNCH_DAEMON_DIR="$TEST_ROOT"
RUNNER_URL=https://github.com/Acme/Repo
sudo() { [[ "$*" == "launchctl print system/com.github.runner-1" ]] || exit 99; }
if ! configure_runner 1; then exit 98; fi
printf '%s' "${STATUS_RESULT[0]}"
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("skipped", result.stdout)

    def test_registration_pat_uses_stdin_and_invalid_json_fails(self):
        body = '''
GITHUB_PAT_VALUE=dummy-private-pat; API_ENDPOINT=https://example.invalid/token
curl() {
  printf '%s\\n' "$@" > "$TEST_ROOT/args"
  cat > "$TEST_ROOT/config"
  printf '%s' "$TEST_RESPONSE"
}
if fetch_registration_token; then printf '%s' "$FETCHED_TOKEN"; else exit 23; fi
'''
        result = self.shell("runner-ephemeral", body, TEST_RESPONSE='{"token":"dummy-registration-token"}')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "dummy-registration-token")
        self.assertNotIn("dummy-private-pat", (self.root / "args").read_text())
        self.assertIn("dummy-private-pat", (self.root / "config").read_text())
        self.assertNotIn("dummy-private-pat", result.stderr)
        for payload in ('{"message":"rate limited"}', '{"token":null}', '{"token":""}', 'bad json'):
            with self.subTest(payload=payload):
                self.assertEqual(self.shell("runner-ephemeral", body, TEST_RESPONSE=payload).returncode, 23)

    def test_new_binaries_resolve_and_download_only_once(self):
        result = self.shell("runner-setup", '''
RUNNER_ROOT="$TEST_ROOT"; RUNNERS=4
resolve_latest_release() { echo resolve; }
download_tarball() { echo download; }
prepare_runner_archive
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["resolve", "download"])

    def test_verified_cached_archive_avoids_network_and_corruption_refetches(self):
        source = self.root / "source.tar.gz"
        with tarfile.open(source, "w:gz") as archive:
            data = b"#!/bin/bash\n"
            item = tarfile.TarInfo("config.sh")
            item.size = len(data)
            archive.addfile(item, io.BytesIO(data))
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        cache = self.root / "Library/Caches/github-actions-runner/runner.tar.gz"
        cache.parent.mkdir(parents=True)
        cache.write_bytes(source.read_bytes())
        (self.root / "stage").mkdir()
        body = '''
REAL_HOME="$TEST_ROOT"; WORK_TMPDIR="$TEST_ROOT/stage"
TARBALL_NAME=runner.tar.gz; EXPECTED_SHA="$TEST_DIGEST"
RUNNER_VERSION=test; ARCH_TAG=arm64; DOWNLOAD_URL=https://example.invalid/test
as_user() { "$@"; }
xattr() { :; }
curl() { echo fetch >> "$TEST_ROOT/network"; while [[ "$1" != -o ]]; do shift; done; cp "$TEST_ROOT/source.tar.gz" "$2"; }
download_tarball
'''
        result = self.shell("runner-setup", body, TEST_DIGEST=digest)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root / "network").exists())
        (self.root / "stage/runner.tar.gz").unlink()
        cache.write_bytes(b"corrupted archive")
        result = self.shell("runner-setup", body, TEST_DIGEST=digest)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / "network").read_text(), "fetch\n")
        self.assertEqual(hashlib.sha256(cache.read_bytes()).hexdigest(), digest)

    def test_ephemeral_identity_survives_registration_file_removal(self):
        (self.root / ".runner").write_text(json.dumps({"agentName": "host-runner-1", "gitHubUrl": "https://github.com/acme"}))
        result = self.shell("runner-ephemeral", '''
DIR="$TEST_ROOT"; RUNNER_URL=https://github.com/acme
read_saved_identity
rm "$DIR/.runner"
read_saved_identity
printf '%s' "$RUNNER_NAME"
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "host-runner-1")

    def test_ephemeral_runner_group_survives_registration_file_removal(self):
        (self.root / ".runner").write_text(json.dumps({
            "agentName": "host-runner-1", "poolName": "ARM build runners",
        }))
        result = self.shell("runner-ephemeral", '''
DIR="$TEST_ROOT"; RUNNER_URL=https://github.com/acme
read_saved_identity
rm "$DIR/.runner"
read_saved_identity
printf '%s' "$RUNNER_GROUP"
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "ARM build runners")

    def test_ephemeral_cycles_register_in_saved_group_after_registration_disappears(self):
        group = 'Build "ARM" runners'
        (self.root / ".runner").write_text(json.dumps({
            "agentName": "host-runner-1", "poolName": group,
        }))
        (self.root / "bin").mkdir()
        for name, code in (
            ("config.sh", "printf '%s\\0' \"$@\" >> config-args\ntouch .runner"),
            ("bin/runsvc.sh", "rm .runner"),
        ):
            path = self.root / name
            path.write_text("#!/bin/bash\n" + code + "\n")
            path.chmod(0o755)
        result = self.shell("runner-ephemeral", '''
DIR="$TEST_ROOT"; RUNNER_URL=https://github.com/acme
fetch_registration_token() { FETCHED_TOKEN=secret; }
if ! run_cycle || ! run_cycle; then exit 99; fi
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        arguments = (self.root / "config-args").read_bytes().decode().split("\0")
        groups = [arguments[index + 1] for index, value in enumerate(arguments) if value == "--runnergroup"]
        self.assertEqual(groups, [group, group])
        self.assertFalse((self.root / ".runner").exists())

    def test_ephemeral_registration_without_saved_group_omits_group_override(self):
        (self.root / ".runner").write_text(json.dumps({"agentName": "host-runner-1"}))
        (self.root / "bin").mkdir()
        for name, code in (
            ("config.sh", "printf '%s\\0' \"$@\" > config-args\ntouch .runner"),
            ("bin/runsvc.sh", "exit 0"),
        ):
            path = self.root / name
            path.write_text("#!/bin/bash\n" + code + "\n")
            path.chmod(0o755)
        result = self.shell("runner-ephemeral", '''
DIR="$TEST_ROOT"; RUNNER_URL=https://github.com/acme
fetch_registration_token() { FETCHED_TOKEN=secret; }
if ! run_cycle; then exit 99; fi
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        arguments = (self.root / "config-args").read_bytes().decode().split("\0")
        self.assertNotIn("--runnergroup", arguments)

    def test_ephemeral_wipe_keeps_operator_configuration(self):
        for name in (".env", ".path", ".runner", ".credentials", ".credentials_rsaparams"):
            (self.root / name).write_text("data")
        (self.root / "_work").mkdir()
        result = self.shell("runner-ephemeral", 'DIR="$TEST_ROOT"; wipe_state')
        self.assertEqual(result.returncode, 0, result.stderr)
        for name in (".env", ".path"):
            self.assertTrue((self.root / name).exists())
        for name in (".runner", ".credentials", ".credentials_rsaparams", "_work"):
            self.assertFalse((self.root / name).exists())

    def test_reregistration_preserves_tool_path_when_config_rewrites_it(self):
        (self.root / "bin").mkdir()
        tool_dir = self.root / "custom-tools"
        tool_dir.mkdir()
        tool = tool_dir / "custom-compiler"
        tool.write_text("#!/bin/bash\nexit 0\n")
        tool.chmod(0o755)
        saved_path = f"{tool_dir}:/usr/bin:/bin"
        (self.root / ".path").write_text(saved_path + "\n")
        scripts = {
            "config.sh": 'echo "$PATH" > .path\ntouch .runner',
            "bin/runsvc.sh": 'export PATH="$(cat .path)"\ncommand -v custom-compiler > resolved-compiler',
        }
        for name, code in scripts.items():
            path = self.root / name
            path.write_text("#!/bin/bash\n" + code + "\n")
            path.chmod(0o755)
        result = self.shell("runner-ephemeral", '''
DIR="$TEST_ROOT"; RUNNER_URL=https://github.com/acme
fetch_registration_token() { FETCHED_TOKEN=secret; }
if ! run_cycle; then exit 99; fi
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / ".path").read_text().strip(), saved_path)
        self.assertEqual((self.root / "resolved-compiler").read_text().strip(), str(tool))

    def test_ephemeral_cleanup_failure_prevents_registration(self):
        result = self.shell("runner-ephemeral", '''
DIR="$TEST_ROOT"; RUNNER_URL=https://github.com/acme
fetch_registration_token() { FETCHED_TOKEN=secret; }
wipe_state() { return 1; }
if run_cycle; then exit 99; fi
[[ -z "$FETCHED_TOKEN" ]]
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("refusing re-registration", result.stderr)

    def test_listener_failure_is_reported_for_backoff(self):
        (self.root / "bin").mkdir()
        for name, code in (("config.sh", "touch .runner"), ("bin/runsvc.sh", "exit 7")):
            p = self.root / name
            p.write_text("#!/bin/bash\n" + code + "\n")
            p.chmod(0o755)
        result = self.shell("runner-ephemeral", '''
DIR="$TEST_ROOT"; RUNNER_URL=https://github.com/acme
fetch_registration_token() { FETCHED_TOKEN=secret; }
if run_cycle; then exit 99; fi
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("rc=7", result.stderr)


if __name__ == "__main__":
    unittest.main()
