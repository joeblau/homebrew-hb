"""Regression tests for cache wiring; no Homebrew, launchd, or real S3 access."""

from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "runner-cache"


class RunnerCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="runner-cache-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for directory in ("config/scopes", "bin", "data", "logs"):
            (self.root / directory).mkdir(parents=True, exist_ok=True)
        self.env = self.root / "config/minio.env"
        self.env.write_text(
            'MINIO_ROOT_USER="fixture-root"\n'
            'MINIO_ROOT_PASSWORD="fixture-root-secret"\n'
            'MINIO_PROMETHEUS_AUTH_TYPE="public"\n'
            'CACHE_PORT="19000"\nCACHE_CONSOLE_PORT="19001"\n'
        )

    def shell(self, body):
        prelude = r'''
source "$1"
CACHE_ROOT="$2"
SCOPES_DIR="${CACHE_ROOT}/config/scopes"
MINIO_ENV_FILE="${CACHE_ROOT}/config/minio.env"
WRAPPER_PATH="${CACHE_ROOT}/bin/run-minio.sh"
PLIST_DEST="${CACHE_ROOT}/fixture.plist"
LOG_DIR="${CACHE_ROOT}/logs"
detect_platform() { :; }
detect_user() { :; }
require_cmds() { :; }
ensure_sudo() { :; }
as_user() { "$@"; }
sudo() { printf 'Unexpected sudo invocation\n' >&2; return 90; }
brew() { printf 'Unexpected brew invocation\n' >&2; return 91; }
'''
        return subprocess.run(
            ["/bin/bash", "-c", prelude + "\n" + body, "test", str(SCRIPT), str(self.root)],
            text=True, capture_output=True, check=False,
        )

    def install_scope_fixture(self):
        path = self.root / "config/scopes/repo-acme-project.env"
        path.write_text(
            'SCOPE_KIND="repo"\nSCOPE="acme/project"\n'
            'BUCKET="actions-cache-repo-acme-project"\n'
            'POLICY_NAME="cache-repo-acme-project"\n'
            'ACCESS_KEY="fixture-scope-access"\nSECRET_KEY="fixture-scope-secret"\n'
        )
        return path

    def test_env_outputs_usable_s3_settings_without_secrets_or_github_overrides(self):
        self.install_scope_fixture()
        result = self.shell('parse_args env --repo acme/project; cmd_env')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(dict(line.split("=", 1) for line in result.stdout.splitlines()), {
            "RUNNER_CACHE_ENDPOINT": "127.0.0.1", "RUNNER_CACHE_PORT": "19000",
            "RUNNER_CACHE_BUCKET": "actions-cache-repo-acme-project",
            "RUNNER_CACHE_INSECURE": "true",
        })
        self.assertIn("tespkg/actions-cache@v1", result.stderr)
        self.assertIn("port: 19000", result.stderr)
        self.assertIn("${{ secrets.RUNNER_CACHE_SECRET_KEY }}", result.stderr)
        self.assertNotIn("ACTIONS_", result.stdout)
        for secret in ("fixture-root", "fixture-root-secret", "fixture-scope-access", "fixture-scope-secret"):
            self.assertNotIn(secret, result.stdout + result.stderr)

    def test_distinct_scopes_have_distinct_valid_buckets(self):
        scopes = ["a-b/c", "a/b-c", "a/b_c", "a/b.c", "a/" + "x" * 99,
                  "a/" + "x" * 98 + "y"]
        buckets = []
        for scope in scopes:
            result = self.shell('parse_args env --repo ' + shlex.quote(scope) + '; resolve_scope; printf "%s" "$BUCKET"')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertRegex(result.stdout, r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")
            buckets.append(result.stdout)
        self.assertEqual(len(set(buckets)), len(scopes))

    def test_scope_case_is_canonical_and_legacy_identity_must_match(self):
        self.install_scope_fixture()
        result = self.shell('parse_args env --repo ACME/PROJECT; resolve_scope; printf "%s" "$SCOPE_SLUG"')
        self.assertEqual(result.stdout, "repo-acme-project")
        legacy = self.root / "config/scopes/repo-a-b-c.env"
        legacy.write_text('SCOPE_KIND="repo"\nSCOPE="a-b/c"\nBUCKET="actions-cache-repo-a-b-c"\nPOLICY_NAME="cache-repo-a-b-c"\n')
        result = self.shell('parse_args env --repo a/b-c; resolve_scope; printf "%s" "$BUCKET"')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotEqual(result.stdout, "actions-cache-repo-a-b-c")

    def test_invalid_scope_and_ports_fail_before_any_provisioning(self):
        arguments = [
            'install --repo acme/"$(touch escaped)"',
            'env --org "acme;echo bad"', 'env --repo ../bad/path',
            'install --org acme --port 80',
            'install --org acme --port 999999999999999999999999',
            'install --org acme --console-port 9000',
            'install --org acme --port not-a-port',
        ]
        for args in arguments:
            with self.subTest(args=args):
                # shell syntax is passed as data to parse_args, not executed.
                tokens = shlex.split(args)
                result = self.shell('parse_args ' + ' '.join(shlex.quote(token) for token in tokens))
                self.assertEqual(result.returncode, 2, result.stderr)
        result = self.shell('parse_args install --org acme --port 09000; printf "%s" "$PORT"')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "9000")

    def test_wrapper_exports_credentials_and_binds_both_ports_to_loopback(self):
        fake_minio = self.root / "bin/minio"
        fake_minio.write_text('#!/bin/bash\nprintf "%s\\n" "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" "$MINIO_PROMETHEUS_AUTH_TYPE" "$@"\n')
        fake_minio.chmod(0o755)
        with self.env.open("a") as f:
            f.write("MINIO_BIN=" + shlex.quote(str(fake_minio)) + "\n")
        result = self.shell('write_minio_wrapper; "$WRAPPER_PATH"')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [
            "fixture-root", "fixture-root-secret", "public", "server",
            "--address", "127.0.0.1:19000", "--console-address",
            "127.0.0.1:19001", str(self.root / "data"),
        ])
        self.assertNotIn("fixture-root-secret", (self.root / "bin/run-minio.sh").read_text())

    def test_admin_credentials_enter_child_environment_without_secret_argv(self):
        fake_mc = self.root / "bin/mc"
        fake_mc.write_text('#!/bin/bash\nprintf "%s\\n" "$MC_HOST_cache" "$@"\n')
        fake_mc.chmod(0o755)
        result = self.shell(r'''
export PATH="${CACHE_ROOT}/bin:${PATH}"
as_user() { printf '%s\n' "$@" > "${CACHE_ROOT}/argv"; "$@"; }
mc_admin ls cache
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("http://fixture-root:fixture-root-secret@127.0.0.1:19000", result.stdout)
        self.assertNotIn("fixture-root-secret", (self.root / "argv").read_text())

    def test_failed_policy_creation_stops_before_user_creation(self):
        result = self.shell(r'''
parse_args install --repo acme/project
resolve_scope
mc_admin() {
  printf '%s\n' "$*" >> "${CACHE_ROOT}/operations"
  [[ "$1 $2 $3" != "admin policy create" ]]
}
provision_scope
''')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refusing to provision", result.stderr)
        self.assertNotIn("admin user", (self.root / "operations").read_text())
        self.assertEqual(list((self.root / "config/scopes").glob("*.env")), [])

    def test_reinstall_reconciles_user_and_policy_with_credentials_on_stdin(self):
        self.install_scope_fixture()
        result = self.shell(r'''
parse_args install --repo acme/project
resolve_scope
mc_admin() {
  printf '%s\n' "$*" >> "${CACHE_ROOT}/operations"
  if [[ "$1 $2 $3" == "admin user add" ]]; then
    cat > "${CACHE_ROOT}/user-stdin"
  fi
}
provision_scope
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        operations = (self.root / "operations").read_text()
        self.assertIn("admin user add cache", operations)
        self.assertIn("admin policy attach", operations)
        self.assertNotIn("fixture-scope-secret", operations + result.stderr + result.stdout)
        self.assertEqual((self.root / "user-stdin").read_text(), "fixture-scope-access\nfixture-scope-secret\n")

    def test_legacy_bucket_collision_fails_closed(self):
        first = self.install_scope_fixture()
        (first.parent / "repo-other-project.env").write_text(first.read_text().replace('SCOPE="acme/project"', 'SCOPE="other/project"'))
        result = self.shell('parse_args install --repo acme/project; resolve_scope; verify_bucket_scope')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("multiple scopes", result.stderr)

    def test_start_health_failure_and_bootout_timeout_are_failures(self):
        (self.root / "fixture.plist").touch()
        result = self.shell('sudo() { :; }; daemon_loaded() { :; }; wait_healthy() { return 1; }; cmd_start')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("did not become healthy", result.stderr)
        result = self.shell('sudo() { :; }; sleep() { :; }; bootout_system fixture')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Timed out", result.stderr)

    def test_metrics_do_not_fabricate_hit_rate_from_s3_errors(self):
        result = self.shell(r'''
curl() {
  printf '%s\n' 'minio_s3_requests_total{api="getobject"} 10' 'minio_s3_requests_4xx_errors_total{api="getobject"} 5'
}
cmd_metrics
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Cache hit rate: unavailable", result.stdout)
        self.assertNotIn("66.7%", result.stdout)
        self.assertNotIn("Approx.", result.stdout)

    def test_keep_data_preserves_credentials_and_data_for_reinstall(self):
        self.install_scope_fixture()
        blob = self.root / "data/blob"
        blob.write_text("cached artifact")
        result = self.shell(r'''
sudo() {
  [[ "$1" == "rm" ]] || return 90
  shift
  /bin/rm "$@"
}
daemon_loaded() { return 1; }
parse_args uninstall --yes --keep-data
cmd_uninstall
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.env.exists())
        self.assertTrue(blob.exists())
        self.assertFalse((self.root / "bin").exists())


class RunnerCacheClientTests(unittest.TestCase):
    """Client mode: configuration-only remote S3 backend; no local services."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="runner-cache-client-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for directory in ("config/scopes", "config/clients", "bin", "data", "logs"):
            (self.root / directory).mkdir(parents=True, exist_ok=True)

    def shell(self, body, stdin=None):
        prelude = r'''
source "$1"
CACHE_ROOT="$2"
SCOPES_DIR="${CACHE_ROOT}/config/scopes"
CLIENTS_DIR="${CACHE_ROOT}/config/clients"
MINIO_ENV_FILE="${CACHE_ROOT}/config/minio.env"
WRAPPER_PATH="${CACHE_ROOT}/bin/run-minio.sh"
PLIST_DEST="${CACHE_ROOT}/fixture.plist"
LOG_DIR="${CACHE_ROOT}/logs"
detect_platform() { :; }
detect_user() { :; }
require_cmds() { :; }
ensure_sudo() { :; }
as_user() { "$@"; }
sudo() { printf 'Unexpected sudo invocation\n' >&2; return 90; }
brew() { printf 'Unexpected brew invocation\n' >&2; return 91; }
launchctl() { printf 'Unexpected launchctl invocation\n' >&2; return 92; }
minio() { printf 'Unexpected minio invocation\n' >&2; return 93; }
mc() { printf 'Unexpected mc invocation\n' >&2; return 94; }
'''
        return subprocess.run(
            ["/bin/bash", "-c", prelude + "\n" + body, "test", str(SCRIPT), str(self.root)],
            text=True, capture_output=True, check=False, input=stdin,
        )

    def install_client(self, extra_args="", stdin="ACCESS_KEY=remote-ak-fixture\nSECRET_KEY=remote-sk-fixture\n"):
        return self.shell(
            "parse_args client install --repo acme/project --endpoint cache.internal.lan "
            "--port 9443 --region eu-west-1 " + extra_args + "; cmd_client_install",
            stdin=stdin,
        )

    def client_config_path(self):
        configs = list((self.root / "config/clients").glob("*.env"))
        self.assertEqual(len(configs), 1)
        return configs[0]

    def test_client_install_stores_remote_config_without_secrets_or_local_services(self):
        result = self.install_client()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Unexpected", result.stderr)
        config = self.client_config_path().read_text()
        for expected in ('SCOPE="acme/project"', 'REMOTE_ENDPOINT="cache.internal.lan"',
                         'REMOTE_PORT="9443"', 'REMOTE_TLS="true"', 'REMOTE_REGION="eu-west-1"',
                         'REMOTE_PATH_STYLE="true"'):
            self.assertIn(expected, config)
        self.assertIn('BUCKET="actions-cache-repo-acme-project-', config)
        credentials = self.client_config_path().with_suffix(".credentials")
        self.assertTrue(credentials.exists())
        self.assertEqual(credentials.stat().st_mode & 0o777, 0o600)
        self.assertEqual(credentials.read_text(),
                         '# runner-cache credentials v2\nACCESS_KEY=remote-ak-fixture\nSECRET_KEY=remote-sk-fixture\n')
        for secret in ("remote-ak-fixture", "remote-sk-fixture"):
            self.assertNotIn(secret, config + result.stdout + result.stderr)
        # No local daemon or MinIO state is created.
        self.assertFalse((self.root / "config/minio.env").exists())
        self.assertEqual(list((self.root / "config/scopes").glob("*.env")), [])

    def test_client_env_emits_remote_s3_integration_without_secrets(self):
        self.assertEqual(self.install_client().returncode, 0)
        result = self.shell('parse_args client env --repo acme/project; cmd_client_env')
        self.assertEqual(result.returncode, 0, result.stderr)
        bucket = [line.split("=", 1)[1].strip('"') for line in
                  self.client_config_path().read_text().splitlines()
                  if line.startswith("BUCKET=")][0]
        self.assertEqual(dict(line.split("=", 1) for line in result.stdout.splitlines()), {
            "RUNNER_CACHE_ENDPOINT": "cache.internal.lan",
            "RUNNER_CACHE_PORT": "9443",
            "RUNNER_CACHE_BUCKET": bucket,
            "RUNNER_CACHE_INSECURE": "false",
            "RUNNER_CACHE_REGION": "eu-west-1",
            "RUNNER_CACHE_PATH_STYLE": "true",
        })
        self.assertNotIn("ACTIONS_", result.stdout)
        self.assertIn("tespkg/actions-cache@v1", result.stderr)
        self.assertIn("endpoint: cache.internal.lan", result.stderr)
        self.assertIn("region: eu-west-1", result.stderr)
        self.assertIn("insecure: false", result.stderr)
        self.assertIn("${{ secrets.RUNNER_CACHE_SECRET_KEY }}", result.stderr)
        self.assertIn("SCCACHE_BUCKET=" + bucket, result.stderr)
        self.assertIn("SCCACHE_ENDPOINT=cache.internal.lan:9443", result.stderr)
        self.assertIn("ACTIONS_CACHE_URL", result.stderr)  # only as a "never redirect" warning
        for secret in ("remote-ak-fixture", "remote-sk-fixture"):
            self.assertNotIn(secret, result.stdout + result.stderr)

    def test_credentials_metacharacters_are_literal_and_never_executed(self):
        marker = self.root / "executed"
        secret = f"'quoted\"$UNSET `touch {marker}` $(touch {marker})\\end'"
        result = self.install_client(stdin=f"ACCESS_KEY=literal$key\nSECRET_KEY={secret}")
        self.assertEqual(result.returncode, 0, result.stderr)
        stored = self.client_config_path().with_suffix(".credentials").read_text()
        self.assertIn(f"SECRET_KEY={secret}\n", stored)
        result = self.shell('parse_args client env --repo acme/project; cmd_client_env')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(marker.exists())
        self.assertNotIn(secret, result.stdout + result.stderr)

    def test_legacy_credential_file_is_not_sourced(self):
        marker = self.root / "executed"
        external = self.root / "old.credentials"
        external.write_text(f'ACCESS_KEY="$(touch {marker})"\nSECRET_KEY="valid"\n')
        result = self.install_client(extra_args="--credentials-file " + shlex.quote(str(external)), stdin=None)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(marker.exists())

    def test_virtual_host_mode_rejects_unsupported_action_snippet(self):
        self.assertEqual(self.install_client(extra_args="--path-style off").returncode, 0)
        result = self.shell('parse_args client env --repo acme/project; cmd_client_env')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("uses: tespkg", result.stderr)
        self.assertIn("no addressing-mode input", result.stderr)

    def test_client_defaults_and_no_tls_port_resolution(self):
        result = self.shell(
            'parse_args client install --org acme --endpoint 10.0.0.5; '
            'printf "%s %s %s" "$CLIENT_PORT" "$TLS" "$PATH_STYLE"')
        self.assertEqual(result.stdout, "443 1 1")
        result = self.shell(
            'parse_args client install --org acme --endpoint 10.0.0.5 --no-tls --path-style off; '
            'printf "%s %s %s" "$CLIENT_PORT" "$TLS" "$PATH_STYLE"')
        self.assertEqual(result.stdout, "9000 0 0")

    def test_client_install_rejects_invalid_options_before_writing_anything(self):
        arguments = [
            "client install --repo acme/project",
            "client install --repo acme/project --endpoint http://cache.internal.lan",
            "client install --repo acme/project --endpoint cache.internal.lan:9000",
            "client install --repo acme/project --endpoint 'bad host'",
            "client install --repo acme/project --endpoint cache.internal.lan --port 0",
            "client install --repo acme/project --endpoint cache.internal.lan --port not-a-port",
            "client install --repo acme/project --endpoint cache.internal.lan --region 'bad region'",
            "client install --repo acme/project --endpoint cache.internal.lan --path-style maybe",
            "client install --repo acme/project --endpoint cache.internal.lan --bucket Bad_Bucket",
            "client install --endpoint cache.internal.lan",
            "client env --org acme --repo acme/project",
        ]
        for args in arguments:
            with self.subTest(args=args):
                tokens = shlex.split(args)
                result = self.shell('parse_args ' + ' '.join(shlex.quote(token) for token in tokens))
                self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(list((self.root / "config/clients").glob("*.env")), [])

    def test_client_install_requires_credentials_and_reports_their_absence(self):
        result = self.shell(
            'parse_args client install --repo acme/project --endpoint cache.internal.lan; '
            'cmd_client_install',
            stdin="",
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("credentials", result.stderr)
        self.assertEqual(list((self.root / "config/clients").glob("*.env")), [])

    def test_client_install_references_external_credentials_file(self):
        external = self.root / "elsewhere.credentials"
        external.write_text('ACCESS_KEY="external-ak-fixture"\nSECRET_KEY="external-sk-fixture"\n')
        external.chmod(0o600)
        result = self.install_client(
            extra_args="--credentials-file " + shlex.quote(str(external)), stdin=None)
        self.assertEqual(result.returncode, 0, result.stderr)
        config = self.client_config_path().read_text()
        self.assertIn("CREDENTIALS_FILE=" + str(external), config)
        self.assertNotIn("external-ak-fixture", config + result.stdout + result.stderr)
        # No managed credential copy is made.
        self.assertEqual(list((self.root / "config/clients").glob("*.credentials")), [])
        result = self.shell('parse_args client env --repo acme/project; cmd_client_env')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("external-sk-fixture", result.stdout + result.stderr)
        # uninstall removes the config but never the external credentials file.
        result = self.shell('parse_args client uninstall --repo acme/project; cmd_client_uninstall')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(list((self.root / "config/clients").glob("*.env")), [])
        self.assertTrue(external.exists())

    def test_client_install_fails_when_referenced_credentials_file_is_missing(self):
        result = self.install_client(
            extra_args="--credentials-file " + str(self.root / "nope.credentials"), stdin=None)
        self.assertEqual(result.returncode, 1)
        self.assertIn("credentials", result.stderr.lower())
        self.assertEqual(list((self.root / "config/clients").glob("*.env")), [])

    def test_client_env_fails_closed_when_credentials_disappear(self):
        self.assertEqual(self.install_client().returncode, 0)
        self.client_config_path().with_suffix(".credentials").unlink()
        result = self.shell('parse_args client env --repo acme/project; cmd_client_env')
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("credentials", result.stderr.lower())

    def test_client_env_reports_unconfigured_scope(self):
        result = self.shell('parse_args client env --repo acme/project; cmd_client_env')
        self.assertEqual(result.returncode, 1)
        self.assertIn("not configured", result.stderr)

    def test_client_uninstall_removes_managed_config_and_credentials(self):
        self.assertEqual(self.install_client().returncode, 0)
        managed = self.client_config_path().with_suffix(".credentials")
        result = self.shell('parse_args client uninstall --repo acme/project; cmd_client_uninstall')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(list((self.root / "config/clients").glob("*.env")), [])
        self.assertFalse(managed.exists())

    def test_local_mode_files_and_env_are_untouched_by_client_presence(self):
        # A scope can exist in both modes; local env output is unchanged.
        scopes = self.root / "config/scopes"
        (scopes / "repo-acme-project.env").write_text(
            'SCOPE_KIND="repo"\nSCOPE="acme/project"\n'
            'BUCKET="actions-cache-repo-acme-project"\n'
            'POLICY_NAME="cache-repo-acme-project"\n'
            'ACCESS_KEY="fixture-scope-access"\nSECRET_KEY="fixture-scope-secret"\n'
        )
        (self.root / "config/minio.env").write_text('CACHE_PORT="19000"\nCACHE_CONSOLE_PORT="19001"\n')
        self.assertEqual(self.install_client().returncode, 0)
        result = self.shell('parse_args env --repo acme/project; cmd_env')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(dict(line.split("=", 1) for line in result.stdout.splitlines()), {
            "RUNNER_CACHE_ENDPOINT": "127.0.0.1", "RUNNER_CACHE_PORT": "19000",
            "RUNNER_CACHE_BUCKET": "actions-cache-repo-acme-project",
            "RUNNER_CACHE_INSECURE": "true",
        })


if __name__ == "__main__":
    unittest.main()
