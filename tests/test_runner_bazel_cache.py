"""Regression tests for the Bazel remote cache tool; no Homebrew, launchd, or network access."""

import hashlib
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "runner-bazel-cache"


def scope_slug(kind, scope):
    digest = hashlib.sha256(f"{kind}:{scope}".lower().encode()).hexdigest()[:16]
    raw = scope.replace("/", "-") if kind == "repo" else scope
    slug = "".join(c if c.isalnum() else "-" for c in raw.lower())
    while "--" in slug:
        slug = slug.replace("--", "-")
    slug = slug.strip("-")
    return f"{kind}-{slug[:27]}-{digest}"


class RunnerBazelCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="runner-bazel-cache-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for directory in ("config/scopes", "bin", "data", "logs"):
            (self.root / directory).mkdir(parents=True, exist_ok=True)
        self.slug = scope_slug("repo", "acme/project")

    def shell(self, body):
        prelude = r'''
source "$1"
CACHE_ROOT="$2"
SCOPES_DIR="${CACHE_ROOT}/config/scopes"
LAUNCH_DAEMON_DIR="${CACHE_ROOT}"
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

    def scope_fixture(self, slug=None, scope="acme/project", port="19100", size="25"):
        slug = slug or scope_slug("repo", scope)
        path = self.root / f"config/scopes/{slug}.env"
        path.write_text(
            'SCOPE_KIND="repo"\n'
            f'SCOPE="{scope}"\n'
            f'PORT="{port}"\n'
            f'MAX_SIZE_GIB="{size}"\n'
            'BAZEL_REMOTE_BIN="/usr/local/bin/bazel-remote"\n'
            'USERNAME="bazel-fixture"\n'
            'PASSWORD="fixture-scope-secret"\n'
            f'REMOTE_CACHE_URL="http://bazel-fixture:fixture-scope-secret@127.0.0.1:{port}"\n'
            'CREATED_AT="2026-01-01T00:00:00Z"\n'
        )
        return path

    def test_help_and_unknown_command_exit_codes(self):
        result = subprocess.run(
            ["/bin/bash", str(SCRIPT), "--help"], text=True, capture_output=True, check=False
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("USAGE:", result.stderr)
        result = subprocess.run(
            ["/bin/bash", str(SCRIPT), "frobnicate"], text=True, capture_output=True, check=False
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("Unknown command", result.stderr)

    def test_env_outputs_exact_remote_cache_flag_without_secrets(self):
        self.scope_fixture()
        result = self.shell('parse_args env --repo acme/project; cmd_env')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(dict(line.split("=", 1) for line in result.stdout.splitlines()), {
            "RUNNER_BAZEL_CACHE_ENDPOINT": "127.0.0.1",
            "RUNNER_BAZEL_CACHE_PORT": "19100",
            "RUNNER_BAZEL_CACHE_MAX_SIZE_GIB": "25",
            "RUNNER_BAZEL_CACHE_REMOTE_CACHE_FLAG": "--remote_cache=http://127.0.0.1:19100",
        })
        self.assertIn(".bazelrc", result.stderr)
        self.assertIn("BAZEL_REMOTE_CACHE_URL", result.stderr)
        self.assertIn("secrets.BAZEL_REMOTE_CACHE_URL", result.stderr)
        self.assertIn(str(self.root / f"config/scopes/{self.slug}.env"), result.stderr)
        for secret in ("fixture-scope-secret", "bazel-fixture"):
            self.assertNotIn(secret, result.stdout + result.stderr)

    def test_env_without_scopes_and_ambiguous_scopes_fail(self):
        result = self.shell('parse_args env; cmd_env')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No bazel cache scope installed", result.stderr)
        self.scope_fixture()
        self.scope_fixture(scope="acme/other")
        result = self.shell('parse_args env; cmd_env')
        self.assertEqual(result.returncode, 2)
        self.assertIn("Multiple scopes", result.stderr)

    def test_distinct_scopes_have_distinct_valid_slugs_and_dirs(self):
        scopes = ["a-b/c", "a/b-c", "a/b_c", "a/b.c", "a/" + "x" * 99,
                  "a/" + "x" * 98 + "y"]
        slugs = []
        dirs = []
        for scope in scopes:
            result = self.shell(
                'parse_args env --repo ' + shlex.quote(scope)
                + '; resolve_scope; printf "%s %s" "$SCOPE_SLUG" "$(data_dir "$SCOPE_SLUG")"'
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            slug, directory = result.stdout.split(" ", 1)
            self.assertRegex(slug, r"^repo-[a-z0-9][a-z0-9-]*-[0-9a-f]{16}$")
            slugs.append(slug)
            dirs.append(directory)
        self.assertEqual(len(set(slugs)), len(scopes))
        self.assertEqual(len(set(dirs)), len(scopes))

    def test_scope_case_is_canonical(self):
        result = self.shell('parse_args env --repo ACME/PROJECT; resolve_scope; printf "%s" "$SCOPE_SLUG"')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, self.slug)

    def test_invalid_scope_ports_and_sizes_fail_before_any_provisioning(self):
        arguments = [
            'install --repo acme/"$(touch escaped)"',
            'env --org "acme;echo bad"', 'env --repo ../bad/path',
            'env --org acme --repo acme/project',
            'install', 'install --port 9100',
            'install --org acme --port 80',
            'install --org acme --port 999999999999999999999999',
            'install --org acme --port not-a-port',
            'install --org acme --max-size 0',
            'install --org acme --max-size abc',
            'install --org acme --max-size 9999',
        ]
        for args in arguments:
            with self.subTest(args=args):
                # shell syntax is passed as data to parse_args, not executed.
                tokens = shlex.split(args)
                result = self.shell('parse_args ' + ' '.join(shlex.quote(token) for token in tokens))
                self.assertEqual(result.returncode, 2, result.stderr)
        result = self.shell('parse_args install --org acme --port 09100 --max-size 050; printf "%s %s" "$PORT_REQUESTED" "$MAX_SIZE_GIB"')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "9100 50")

    def test_wrapper_binds_loopback_with_scope_dir_cap_and_htpasswd(self):
        fake = self.root / "bin/bazel-remote"
        fake.write_text('#!/bin/bash\nprintf "%s\\n" "$@"\n')
        fake.chmod(0o755)
        fixture = self.scope_fixture()
        fixture.write_text(fixture.read_text().replace(
            'BAZEL_REMOTE_BIN="/usr/local/bin/bazel-remote"',
            "BAZEL_REMOTE_BIN=" + shlex.quote(str(fake))))
        result = self.shell(f'write_wrapper "{self.slug}"; "$(wrapper_path "{self.slug}")"')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [
            "--dir", str(self.root / f"data/{self.slug}"),
            "--max_size", "25",
            "--http_address", "127.0.0.1:19100",
            "--htpasswd_file", str(self.root / f"config/{self.slug}.htpasswd"),
        ])
        wrapper = (self.root / f"bin/run-bazel-remote-{self.slug}.sh").read_text()
        self.assertNotIn("fixture-scope-secret", wrapper)

    def test_allocate_port_skips_used_and_rejects_conflicts(self):
        self.scope_fixture(scope="acme/one", port="9092")
        self.scope_fixture(scope="acme/two", port="9093")
        result = self.shell('allocate_port ""')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "9094")
        result = self.shell('allocate_port 19500')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "19500")
        result = self.shell('allocate_port 9093')
        self.assertEqual(result.returncode, 2)
        self.assertIn("already used", result.stderr)

    def test_provision_writes_0600_credentials_and_hashed_htpasswd(self):
        result = self.shell(r'''
openssl() {
  if [[ "$1" == "rand" && "$3" == "4" ]]; then printf 'aa11bb22\n'; return 0; fi
  if [[ "$1" == "rand" ]]; then printf 'ff00ee11dd22cc33ff00ee11dd22cc33\n'; return 0; fi
  if [[ "$1" == "passwd" ]]; then [[ "$(cat)" == "$PASSWORD" ]] || return 96; printf 'STUBHASH\n'; return 0; fi
  return 95
}
parse_args install --repo acme/project --port 19100 --max-size 25
resolve_scope
PORT=19100
provision_scope
stat -f '%Lp' "$(scope_file "$SCOPE_SLUG")" "$(htpasswd_path "$SCOPE_SLUG")"
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["600", "600"])
        scope_text = (self.root / f"config/scopes/{self.slug}.env").read_text()
        self.assertIn('USERNAME="bazel-aa11bb22"', scope_text)
        self.assertIn('PASSWORD="ff00ee11dd22cc33ff00ee11dd22cc33"', scope_text)
        self.assertIn(
            'REMOTE_CACHE_URL="http://bazel-aa11bb22'
            ':ff00ee11dd22cc33ff00ee11dd22cc33@127.0.0.1:19100"',
            scope_text)
        self.assertIn('PORT="19100"', scope_text)
        self.assertIn('MAX_SIZE_GIB="25"', scope_text)
        htpasswd = (self.root / f"config/{self.slug}.htpasswd").read_text()
        self.assertEqual(htpasswd.strip(), "bazel-aa11bb22:STUBHASH")
        self.assertNotIn("ff00ee11dd22cc33ff00ee11dd22cc33", htpasswd)
        self.assertTrue((self.root / f"data/{self.slug}").is_dir())

    def test_reinstall_reuses_credentials_and_repairs_htpasswd(self):
        fixture = self.scope_fixture()
        fixture.chmod(0o644)
        result = self.shell(r'''
openssl() {
  # No new secrets may be generated on reinstall; htpasswd hashing still runs.
  if [[ "$1" == "passwd" ]]; then printf 'STUBHASH\n'; return 0; fi
  return 95
}
parse_args install --repo acme/project
resolve_scope
load_scope "$SCOPE_SLUG"
provision_scope
stat -f '%Lp' "$(scope_file "$SCOPE_SLUG")"
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "600")
        scope_text = fixture.read_text()
        self.assertIn('PASSWORD="fixture-scope-secret"', scope_text)
        htpasswd = (self.root / f"config/{self.slug}.htpasswd").read_text()
        self.assertTrue(htpasswd.startswith("bazel-fixture:"))
        self.assertNotIn("fixture-scope-secret", htpasswd)

    def test_start_health_failure_is_a_failure(self):
        self.scope_fixture()
        (self.root / f"com.github.runner-bazel-cache-{self.slug}.plist").touch()
        result = self.shell(
            'sudo() { :; }; daemon_loaded() { :; }; wait_healthy() { return 1; }; cmd_start')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("did not become healthy", result.stderr)

    def test_status_json_lists_scopes_and_empty_when_uninstalled(self):
        result = self.shell('parse_args status --json; cmd_status')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"installed":false', result.stdout)
        self.assertIn('"scopes":[]', result.stdout)
        self.scope_fixture()
        result = self.shell(r'''
daemon_loaded_n() { return 0; }
curl() { return 1; }
parse_args status --json; cmd_status
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f'"slug":"{self.slug}"', result.stdout)
        self.assertIn('"scope":"acme/project"', result.stdout)
        self.assertIn('"endpoint":"http://127.0.0.1:19100"', result.stdout)
        self.assertIn('"launchd_loaded":true', result.stdout)
        self.assertIn('"healthy":false', result.stdout)
        self.assertIn('"max_size_gib":25', result.stdout)
        self.assertNotIn("fixture-scope-secret", result.stdout + result.stderr)

    def test_keep_data_preserves_credentials_and_data_for_reinstall(self):
        fixture = self.scope_fixture()
        blob_dir = self.root / f"data/{self.slug}"
        blob_dir.mkdir(parents=True)
        blob = blob_dir / "ac-entry"
        blob.write_text("cached action result")
        wrapper = self.root / f"bin/run-bazel-remote-{self.slug}.sh"
        wrapper.write_text("# wrapper\n")
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
        self.assertTrue(fixture.exists())
        self.assertTrue(blob.exists())
        self.assertFalse(wrapper.exists())

    def test_uninstall_removes_scope_and_last_scope_removes_root(self):
        fixture = self.scope_fixture()
        blob_dir = self.root / f"data/{self.slug}"
        blob_dir.mkdir(parents=True)
        (blob_dir / "ac-entry").write_text("cached action result")
        result = self.shell(r'''
sudo() {
  [[ "$1" == "rm" ]] || return 90
  shift
  /bin/rm "$@"
}
daemon_loaded() { return 1; }
parse_args uninstall --repo acme/project --yes
cmd_uninstall
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(fixture.exists())
        self.assertFalse(blob_dir.exists())
        self.assertFalse(self.root.exists())


if __name__ == "__main__":
    unittest.main()
