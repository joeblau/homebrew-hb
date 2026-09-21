"""Regression tests for the sccache compiler cache tool; no Homebrew, launchd, or network access."""

import hashlib
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "runner-sccache"


def scope_slug(kind, scope):
    digest = hashlib.sha256(f"{kind}:{scope}".lower().encode()).hexdigest()[:16]
    raw = scope.replace("/", "-") if kind == "repo" else scope
    slug = "".join(c if c.isalnum() else "-" for c in raw.lower())
    while "--" in slug:
        slug = slug.replace("--", "-")
    slug = slug.strip("-")
    return f"{kind}-{slug[:27]}-{digest}"


class RunnerSccacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="runner-sccache-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for directory in ("config/scopes", "bin", "data"):
            (self.root / directory).mkdir(parents=True, exist_ok=True)
        self.slug = scope_slug("repo", "acme/project")

    def shell(self, body):
        prelude = r'''
source "$1"
CACHE_ROOT="$2"
SCOPES_DIR="${CACHE_ROOT}/config/scopes"
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

    def scope_fixture(self, slug=None, scope="acme/project", port="14226", size="25",
                      sccache_bin="/usr/local/bin/sccache"):
        slug = slug or scope_slug("repo", scope)
        path = self.root / f"config/scopes/{slug}.env"
        path.write_text(
            'SCOPE_KIND="repo"\n'
            f'SCOPE="{scope}"\n'
            f'PORT="{port}"\n'
            f'MAX_SIZE_GIB="{size}"\n'
            f'SCCACHE_BIN="{sccache_bin}"\n'
            'CREATED_AT="2026-01-01T00:00:00Z"\n'
        )
        return path

    def fake_sccache(self, body):
        fake = self.root / "bin/sccache"
        fake.write_text("#!/bin/bash\n" + body + "\n")
        fake.chmod(0o755)
        return fake

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

    def test_env_outputs_wrapper_settings_and_workflow_snippet(self):
        self.scope_fixture()
        result = self.shell('parse_args env --repo acme/project; cmd_env')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(dict(line.split("=", 1) for line in result.stdout.splitlines()), {
            "SCCACHE_BIN": "/usr/local/bin/sccache",
            "SCCACHE_DIR": str(self.root / f"data/{self.slug}"),
            "SCCACHE_CACHE_SIZE": "25G",
            "SCCACHE_SERVER_PORT": "14226",
            "SCCACHE_IDLE_TIMEOUT": "0",
            "RUSTC_WRAPPER": "/usr/local/bin/sccache",
        })
        self.assertIn("$GITHUB_ENV", result.stderr)
        self.assertIn("cargo build", result.stderr)
        self.assertIn("CMAKE_C_COMPILER_LAUNCHER", result.stderr)
        self.assertIn("Swift", result.stderr)
        self.assertIn("uncached", result.stderr)
        self.assertIn(str(self.root / f"config/scopes/{self.slug}.env"), result.stderr)

    def test_env_without_scopes_and_ambiguous_scopes_fail(self):
        result = self.shell('parse_args env; cmd_env')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No sccache scope installed", result.stderr)
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

    def test_provision_writes_0600_scope_config_and_0700_data_dir(self):
        result = self.shell(r'''
parse_args install --repo acme/project --port 14226 --max-size 25
resolve_scope
PORT="$(allocate_port "$PORT_REQUESTED")"
SCCACHE_BIN="/usr/local/bin/sccache"
provision_scope
stat -f '%Lp' "$(scope_file "$SCOPE_SLUG")" "$(data_dir "$SCOPE_SLUG")"
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["600", "700"])
        scope_text = (self.root / f"config/scopes/{self.slug}.env").read_text()
        self.assertIn('SCOPE_KIND="repo"', scope_text)
        self.assertIn('SCOPE="acme/project"', scope_text)
        self.assertIn('PORT="14226"', scope_text)
        self.assertIn('MAX_SIZE_GIB="25"', scope_text)
        self.assertIn('SCCACHE_BIN="/usr/local/bin/sccache"', scope_text)

    def test_reinstall_reuses_fixed_port_and_size(self):
        self.scope_fixture()
        result = self.shell(r'''
parse_args install --repo acme/project --port 15000 --max-size 40
resolve_scope
if [[ -f "$(scope_file "$SCOPE_SLUG")" ]]; then
  load_scope "$SCOPE_SLUG"
  MAX_SIZE_GIB="$MAX_SIZE_GIB_LOADED"
  warn "port/cap fixed at first install"
fi
provision_scope
printf '%s %s\n' "$PORT" "$MAX_SIZE_GIB"
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "14226 25")
        self.assertIn("fixed", result.stderr)

    def test_allocate_port_skips_used_and_rejects_conflicts(self):
        self.scope_fixture(scope="acme/one", port="4226")
        self.scope_fixture(scope="acme/two", port="4227")
        result = self.shell('allocate_port ""')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "4228")
        result = self.shell('allocate_port 19500')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "19500")
        result = self.shell('allocate_port 4227')
        self.assertEqual(result.returncode, 2)
        self.assertIn("already used", result.stderr)

    def test_stats_json_parses_counters_and_disk_usage(self):
        fake = self.fake_sccache(r'''
[[ "$1" == "--show-stats" ]] || exit 92
[[ "$SCCACHE_SERVER_PORT" == "14226" ]] || exit 93
cat <<'OUT'
Compile requests                    100
Compile requests executed            90
Cache hits                           60
Cache hits rate                    66.67 %
Cache misses                         30
Cache write errors                    2
Cache errors                          1
Cache size                        1.2 GiB
Max cache size                   25.0 GiB
OUT
''')
        self.scope_fixture(sccache_bin=str(fake))
        blob_dir = self.root / f"data/{self.slug}"
        blob_dir.mkdir(parents=True)
        (blob_dir / "entry").write_text("cached object")
        result = self.shell('parse_args stats --repo acme/project --json; cmd_stats')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f'"slug":"{self.slug}"', result.stdout)
        self.assertIn('"scope":"acme/project"', result.stdout)
        self.assertIn('"port":14226', result.stdout)
        self.assertIn('"server_reachable":true', result.stdout)
        self.assertIn('"compile_requests":100', result.stdout)
        self.assertIn('"cache_hits":60', result.stdout)
        self.assertIn('"cache_misses":30', result.stdout)
        self.assertIn('"cache_errors":1', result.stdout)
        self.assertIn('"cache_write_errors":2', result.stdout)
        self.assertIn('"max_size_gib":25', result.stdout)
        self.assertIn('"disk_usage_kb":', result.stdout)
        self.assertNotIn('"disk_usage_kb":null', result.stdout)

    def test_stats_server_unreachable_reports_zeros_and_succeeds(self):
        fake = self.fake_sccache('exit 1')
        self.scope_fixture(sccache_bin=str(fake))
        result = self.shell('parse_args stats --repo acme/project --json; cmd_stats')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"server_reachable":false', result.stdout)
        self.assertIn('"compile_requests":0', result.stdout)
        self.assertIn('"cache_hits":0', result.stdout)
        self.assertIn("not reachable", result.stderr)

    def test_stop_invokes_stop_server_with_scope_port_and_is_best_effort(self):
        marker = self.root / "stop-called"
        fake = self.fake_sccache(f'''
printf '%s %s\\n' "$1" "$SCCACHE_SERVER_PORT" > {shlex.quote(str(marker))}
''')
        self.scope_fixture(sccache_bin=str(fake))
        result = self.shell('parse_args stop --repo acme/project; cmd_stop')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(marker.read_text().strip(), "--stop-server 14226")
        self.assertIn("Stopped sccache server", result.stderr)
        marker.unlink()
        result = self.shell(
            f'SCCACHE_BIN_OVERRIDE=1; parse_args stop --repo acme/project; '
            f'SCCACHE_BIN=/nonexistent/sccache; '
            f'load_scope() {{ PORT="14226"; SCCACHE_BIN="/nonexistent/sccache"; MAX_SIZE_GIB="25"; SCOPE_SLUG="$1"; }}; '
            'cmd_stop')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("No running sccache server", result.stderr)

    def test_keep_data_preserves_config_and_data_for_reinstall(self):
        fixture = self.scope_fixture()
        blob_dir = self.root / f"data/{self.slug}"
        blob_dir.mkdir(parents=True)
        blob = blob_dir / "cache-entry"
        blob.write_text("cached object")
        result = self.shell('parse_args uninstall --yes --keep-data; cmd_uninstall')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(fixture.exists())
        self.assertTrue(blob.exists())

    def test_uninstall_removes_scope_and_last_scope_removes_root(self):
        fixture = self.scope_fixture()
        blob_dir = self.root / f"data/{self.slug}"
        blob_dir.mkdir(parents=True)
        (blob_dir / "cache-entry").write_text("cached object")
        result = self.shell('parse_args uninstall --repo acme/project --yes; cmd_uninstall')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(fixture.exists())
        self.assertFalse(blob_dir.exists())
        self.assertFalse(self.root.exists())

    def test_uninstall_requires_confirmation_without_tty(self):
        self.scope_fixture()
        result = self.shell('parse_args uninstall --repo acme/project; cmd_uninstall < /dev/null')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--yes", result.stderr)


if __name__ == "__main__":
    unittest.main()
