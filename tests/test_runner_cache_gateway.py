"""Regression tests for the runner-cache GitHub-protocol cache gateway.

Bash-level tests mock sudo/launchctl/curl the same way as
test_runner_cache.py. Server-level tests import the Python server that
write_gateway_server emits and exercise its RPC handlers and HTTP auth with a
fake S3 backend — no real MinIO, launchd, or network access.
"""

from pathlib import Path
import importlib.util
import json
import shutil
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request


SCRIPT = Path(__file__).resolve().parents[1] / "runner-cache"


class ShellFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="runner-cache-gw-test-")
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
GATEWAY_SERVER_PATH="${CACHE_ROOT}/bin/cache-gateway.py"
GATEWAY_WRAPPER_PATH="${CACHE_ROOT}/bin/run-cache-gateway.sh"
GATEWAY_PLIST_DEST="${CACHE_ROOT}/gateway.plist"
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

    def install_scope_fixture(self, token=None):
        path = self.root / "config/scopes/repo-acme-project.env"
        text = (
            'SCOPE_KIND="repo"\nSCOPE="acme/project"\n'
            'BUCKET="actions-cache-repo-acme-project"\n'
            'POLICY_NAME="cache-repo-acme-project"\n'
            'ACCESS_KEY="fixture-scope-access"\nSECRET_KEY="fixture-scope-secret"\n'
        )
        if token:
            text += 'GATEWAY_TOKEN="%s"\n' % token
        path.write_text(text)
        path.chmod(0o600)
        return path


class GatewayShellTests(ShellFixture):
    def test_gateway_env_emits_actions_wiring_without_token_value(self):
        sf = self.install_scope_fixture(token="fixture-gw-token")
        with self.env.open("a") as f:
            f.write('GATEWAY_PORT="19157"\n')
        result = self.shell('parse_args gateway env --repo acme/project; cmd_gateway_env')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(dict(line.split("=", 1) for line in result.stdout.splitlines()), {
            "ACTIONS_CACHE_URL": "http://127.0.0.1:19157/",
            "ACTIONS_RESULTS_URL": "http://127.0.0.1:19157/",
            "ACTIONS_CACHE_SERVICE_V2": "true",
        })
        self.assertIn("ACTIONS_RUNTIME_TOKEN", result.stderr)
        self.assertIn(str(sf), result.stderr)
        self.assertIn("save-policy", result.stderr)
        for secret in ("fixture-gw-token", "fixture-root-secret", "fixture-scope-secret"):
            self.assertNotIn(secret, result.stdout + result.stderr)

    def test_gateway_env_requires_minted_token(self):
        self.install_scope_fixture()
        result = self.shell('parse_args gateway env --repo acme/project; cmd_gateway_env')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no gateway token", result.stderr)

    def test_gateway_install_mints_token_policy_and_loopback_daemon(self):
        sf = self.install_scope_fixture()
        result = self.shell(r'''
sudo() {
  printf '%s\n' "$*" >> "${CACHE_ROOT}/sudo-ops"
  case "$1 $2" in
    "launchctl print") return 1 ;;
  esac
  case "$1" in
    cp) shift; /bin/cp "$@" ;;
    rm) shift; /bin/rm "$@" ;;
    *) return 0 ;;
  esac
}
sleep() { :; }
curl() { return 0; }
parse_args gateway install --repo acme/project --gateway-port 19157 --save-prefix main- --save-prefix refs/heads/main
cmd_gateway_install
''')
        self.assertEqual(result.returncode, 0, result.stderr)

        scope_text = sf.read_text()
        self.assertIn('GATEWAY_TOKEN="', scope_text)
        self.assertEqual(oct(sf.stat().st_mode & 0o777), "0o600")
        token = next(
            line.split('"')[1] for line in scope_text.splitlines()
            if line.startswith("GATEWAY_TOKEN="))
        self.assertEqual(len(token), 40)

        policy = (self.root / "config/scopes/repo-acme-project.save-policy").read_text()
        self.assertIn("main-", policy)
        self.assertIn("refs/heads/main", policy)

        server = self.root / "bin/cache-gateway.py"
        self.assertTrue(server.exists())
        self.assertIn("/twirp/github.actions.results.api.v1.CacheService/", server.read_text())
        wrapper = (self.root / "bin/run-cache-gateway.sh").read_text()
        self.assertIn("GATEWAY_S3_ENDPOINT", wrapper)
        self.assertIn('GATEWAY_PORT="19157"', self.env.read_text())

        plist = (self.root / "gateway.plist").read_text()
        self.assertIn("com.github.runner-cache-gateway", plist)
        self.assertIn("run-cache-gateway.sh", plist)
        for secret in (token, "fixture-root-secret", "fixture-scope-secret"):
            self.assertNotIn(secret, plist + wrapper + server.read_text())
        self.assertIn("gateway env --repo acme/project", result.stderr)
        sudo_ops = (self.root / "sudo-ops").read_text()
        self.assertIn("launchctl bootstrap system", sudo_ops)

    def test_gateway_install_reuses_token_and_default_policy(self):
        sf = self.install_scope_fixture(token="existing-gw-token")
        body = r'''
sudo() {
  case "$1 $2" in
    "launchctl print") return 1 ;;
  esac
  case "$1" in
    cp) shift; /bin/cp "$@" ;;
    rm) shift; /bin/rm "$@" ;;
    *) return 0 ;;
  esac
}
sleep() { :; }
curl() { return 0; }
parse_args gateway install --repo acme/project
cmd_gateway_install
'''
        first = self.shell(body)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn('GATEWAY_TOKEN="existing-gw-token"', sf.read_text())
        policy = self.root / "config/scopes/repo-acme-project.save-policy"
        self.assertIn("\n*\n", "\n" + policy.read_text())
        policy.write_text("main-\n")
        second = self.shell(body)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(policy.read_text(), "main-\n")
        self.assertIn("Reusing existing gateway token", second.stderr)

    def test_gateway_install_requires_backend_and_scope(self):
        (self.root / "config/minio.env").unlink()
        result = self.shell('parse_args gateway install --repo acme/project; cmd_gateway_install')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Cache backend is not installed", result.stderr)
        self.env.write_text('CACHE_PORT="19000"\nCACHE_CONSOLE_PORT="19001"\n')
        result = self.shell('parse_args gateway install --repo acme/project; cmd_gateway_install')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("is not provisioned", result.stderr)

    def test_gateway_argument_validation(self):
        for args in ("gateway", "gateway bogus", "gateway install",
                     "gateway install --repo acme/project --gateway-port 80",
                     "gateway install --repo acme/project --gateway-port not-a-port",
                     "gateway install --repo acme/project --save-prefix ''"):
            with self.subTest(args=args):
                result = self.shell("parse_args " + args)
                self.assertEqual(result.returncode, 2, result.stderr)
        # Gateway port must differ from the MinIO ports (checked post-config).
        self.install_scope_fixture()
        result = self.shell(
            'parse_args gateway install --repo acme/project --gateway-port 19000; cmd_gateway_install')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must differ from the MinIO ports", result.stderr)

    def test_gateway_status_json_reports_facts_without_secrets(self):
        self.install_scope_fixture(token="fixture-gw-token")
        (self.root / "bin/cache-gateway.py").write_text("# fixture\n")
        result = self.shell(r'''
daemon_loaded_n() { return 0; }
curl() { return 0; }
parse_args gateway status --json
cmd_gateway_status
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report, {
            "daemon": "com.github.runner-cache-gateway",
            "installed": True, "launchd_loaded": True, "healthy": True,
            "url": "http://127.0.0.1:9157/", "authorized_scopes": 1,
        })
        self.assertNotIn("fixture-gw-token", result.stdout + result.stderr)

    def test_gateway_stop_boots_out_only_when_loaded(self):
        result = self.shell(r'''
sudo() { printf '%s\n' "$*" >> "${CACHE_ROOT}/sudo-ops"; }
sleep() { :; }
daemon_loaded() { return 1; }
cmd_gateway_stop
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("nothing to stop", result.stderr)
        self.assertFalse((self.root / "sudo-ops").exists())
        result = self.shell(r'''
sudo() { printf '%s\n' "$*" >> "${CACHE_ROOT}/sudo-ops"; }
sleep() { :; }
sudo_launchctl_print_fails() { return 1; }
daemon_loaded() { return 0; }
bootout_system() { printf 'bootout %s\n' "$1" >> "${CACHE_ROOT}/sudo-ops"; }
cmd_gateway_stop
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("bootout com.github.runner-cache-gateway",
                      (self.root / "sudo-ops").read_text())

    def test_uninstall_removes_gateway_plist_and_preserves_scope_with_keep_data(self):
        sf = self.install_scope_fixture(token="fixture-gw-token")
        (self.root / "gateway.plist").write_text("<plist/>")
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
        self.assertFalse((self.root / "gateway.plist").exists())
        self.assertTrue(sf.exists())
        self.assertTrue(blob.exists())

    def test_help_lists_gateway(self):
        result = self.shell('parse_args --help')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("gateway install", result.stderr)
        self.assertIn("--save-prefix", result.stderr)
        result = self.shell('parse_args gateway --help')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("USAGE:", result.stderr)


class GatewayServerTests(ShellFixture):
    """Import the emitted Python server and exercise it with a fake S3."""

    @classmethod
    def setUpClass(cls):
        cls.tempdir = tempfile.TemporaryDirectory(prefix="runner-cache-gw-srv-")
        cls.addClassCleanup(cls.tempdir.cleanup)
        root = Path(cls.tempdir.name)
        (root / "bin").mkdir()
        result = subprocess.run(
            ["/bin/bash", "-c",
             'source "$1"\nCACHE_ROOT="$2"\n'
             'GATEWAY_SERVER_PATH="${CACHE_ROOT}/bin/cache-gateway.py"\n'
             'as_user() { "$@"; }\n'
             'write_gateway_server',
             "test", str(SCRIPT), str(root)],
            text=True, capture_output=True, check=False)
        if result.returncode != 0:
            raise AssertionError("write_gateway_server failed: " + result.stderr)
        spec = importlib.util.spec_from_file_location(
            "cache_gateway", root / "bin/cache-gateway.py")
        cls.gw = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.gw)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="runner-cache-gw-scope-")
        self.addCleanup(self.temp.cleanup)
        scopes = Path(self.temp.name)
        (scopes / "repo-acme-project.env").write_text(
            'BUCKET="actions-cache-repo-acme-project"\n'
            'ACCESS_KEY="ak"\nSECRET_KEY="sk"\n'
            'GATEWAY_TOKEN="good-token"\n'
        )
        (scopes / "repo-acme-project.save-policy").write_text("main-\nrelease-\n")
        (scopes / "repo-no-token.env").write_text(
            'BUCKET="other"\nACCESS_KEY="x"\nSECRET_KEY="y"\n')
        self.gw.SCOPES_DIR = str(scopes)
        self.gw.S3_ENDPOINT = "http://127.0.0.1:19000"
        self.scope = self.gw.load_scopes(str(scopes))[0]
        self.calls = []
        self.addCleanup(self._restore_s3)
        self._original_s3 = self.gw.s3_request

    def _restore_s3(self):
        self.gw.s3_request = self._original_s3

    def fake_s3(self, responses):
        """responses: dict of (method, object_key) -> (status, body, headers)."""
        def fake(scope, method, object_key=None, query=None, body=b""):
            self.calls.append((method, object_key, query))
            key = (method, object_key)
            if key not in responses:
                return 404, b"", {}
            status, payload, headers = responses[key]
            return status, payload, headers
        self.gw.s3_request = fake

    def test_load_scopes_requires_token_and_reads_policy(self):
        scopes = self.gw.load_scopes(self.gw.SCOPES_DIR)
        self.assertEqual(len(scopes), 1)
        self.assertEqual(scopes[0]["bucket"], "actions-cache-repo-acme-project")
        self.assertEqual(scopes[0]["save_prefixes"], ["main-", "release-"])
        self.assertIs(self.gw.find_scope(scopes, "good-token"), scopes[0])
        self.assertIsNone(self.gw.find_scope(scopes, "wrong-token"))
        self.assertIsNone(self.gw.find_scope(scopes, ""))

    def test_save_policy_prefixes_and_default_open(self):
        self.assertTrue(self.gw.save_allowed(["main-", "release-"], "main-brew"))
        self.assertFalse(self.gw.save_allowed(["main-"], "dev-brew"))
        self.assertTrue(self.gw.save_allowed(["*"], "anything"))
        missing = Path(self.temp.name) / "no-such.save-policy"
        self.assertEqual(self.gw.load_save_prefixes(str(missing)), ["*"])

    def test_presign_url_is_sigv4_for_scope_bucket(self):
        url = self.gw.presign_url("PUT", self.scope, "main-key/v1")
        self.assertTrue(url.startswith(
            "http://127.0.0.1:19000/actions-cache-repo-acme-project/main-key/v1?"))
        self.assertIn("X-Amz-Algorithm=AWS4-HMAC-SHA256", url)
        self.assertIn("X-Amz-Credential=ak%2F", url)
        self.assertIn("X-Amz-Signature=", url)
        self.assertIn("X-Amz-SignedHeaders=host", url)

    def test_create_cache_entry_reserves_only_when_absent(self):
        self.fake_s3({})
        out = self.gw.rpc_create_cache_entry(
            self.scope, {"key": "main-brew", "version": "v1"})
        self.assertTrue(out["ok"])
        self.assertEqual(out["signed_upload_url"], out["signedUploadUrl"])
        self.assertIn("main-brew/v1", out["signed_upload_url"])
        self.fake_s3({("HEAD", "main-brew/v1"): (200, b"", {})})
        out = self.gw.rpc_create_cache_entry(
            self.scope, {"key": "main-brew", "version": "v1"})
        self.assertFalse(out["ok"])

    def test_save_policy_blocks_create_finalize_delete(self):
        self.fake_s3({("HEAD", "dev-brew/v1"): (200, b"", {"Content-Length": "5"})})
        for handler, body in (
            (self.gw.rpc_create_cache_entry, {"key": "dev-brew", "version": "v1"}),
            (self.gw.rpc_finalize_cache_entry,
             {"key": "dev-brew", "version": "v1", "sizeBytes": 5}),
            (self.gw.rpc_delete_cache_entry, {"key": "dev-brew", "version": "v1"}),
        ):
            with self.subTest(handler=handler.__name__):
                with self.assertRaises(self.gw.TwirpError) as ctx:
                    handler(self.scope, body)
                self.assertEqual(ctx.exception.code, "permission_denied")
                self.assertEqual(ctx.exception.status, 403)
        self.assertEqual(self.calls, [])  # denied before any S3 call

    def test_finalize_verifies_size_and_deletes_mismatch(self):
        self.fake_s3({
            ("HEAD", "main-brew/v1"): (200, b"", {"Content-Length": "42"}),
            ("DELETE", "main-brew/v1"): (204, b"", {}),
        })
        out = self.gw.rpc_finalize_cache_entry(
            self.scope, {"key": "main-brew", "version": "v1",
                         "size_bytes": 42})
        self.assertEqual(out["ok"], True)
        self.assertEqual(out["entry_id"], "42")
        with self.assertRaises(self.gw.TwirpError) as ctx:
            self.gw.rpc_finalize_cache_entry(
                self.scope, {"key": "main-brew", "version": "v1",
                             "sizeBytes": 7})
        self.assertEqual(ctx.exception.code, "invalid_argument")
        self.assertIn(("DELETE", "main-brew/v1", None), self.calls)

    def test_finalize_missing_upload_is_not_found(self):
        self.fake_s3({})
        with self.assertRaises(self.gw.TwirpError) as ctx:
            self.gw.rpc_finalize_cache_entry(
                self.scope, {"key": "main-brew", "version": "v1",
                             "size_bytes": 1})
        self.assertEqual(ctx.exception.status, 404)

    def test_get_download_url_exact_restore_prefix_and_miss(self):
        listing = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
            "<Contents><Key>main-brew-aaa/v1</Key>"
            "<LastModified>2026-09-01T00:00:00.000Z</LastModified></Contents>"
            "<Contents><Key>main-brew-bbb/v1</Key>"
            "<LastModified>2026-09-10T00:00:00.000Z</LastModified></Contents>"
            "<Contents><Key>main-brew-old/v0</Key>"
            "<LastModified>2026-09-20T00:00:00.000Z</LastModified></Contents>"
            "</ListBucketResult>"
        ).encode()
        self.fake_s3({
            ("GET", None): (200, listing, {}),
        })
        # Exact hit.
        self.fake_s3({
            ("HEAD", "main-brew/v1"): (200, b"", {}),
            ("GET", None): (200, listing, {}),
        })
        out = self.gw.rpc_get_download_url(
            self.scope, {"key": "main-brew", "version": "v1"})
        self.assertTrue(out["ok"])
        self.assertEqual(out["matched_key"], "main-brew")
        self.assertIn("main-brew/v1", out["signed_download_url"])
        # Prefix restore picks the newest object with the same version.
        self.fake_s3({("GET", None): (200, listing, {})})
        out = self.gw.rpc_get_download_url(
            self.scope, {"key": "main-brew-ccc", "version": "v1",
                         "restoreKeys": ["main-brew-"]})
        self.assertTrue(out["ok"])
        self.assertEqual(out["matchedKey"], "main-brew-bbb")
        # Restore never touches the save policy.
        out = self.gw.rpc_get_download_url(
            self.scope, {"key": "dev-brew", "version": "v1",
                         "restore_keys": ["main-brew-"]})
        self.assertTrue(out["ok"])
        # Total miss.
        self.fake_s3({("GET", None): (404, b"", {})})
        out = self.gw.rpc_get_download_url(
            self.scope, {"key": "main-nope", "version": "v1"})
        self.assertFalse(out["ok"])

    def test_delete_calls_s3(self):
        self.fake_s3({("DELETE", "main-brew/v1"): (204, b"", {})})
        out = self.gw.rpc_delete_cache_entry(
            self.scope, {"key": "main-brew", "version": "v1"})
        self.assertTrue(out["ok"])
        self.assertIn(("DELETE", "main-brew/v1", None), self.calls)

    def test_http_auth_and_rpc_roundtrip(self):
        self.fake_s3({})
        gw = self.gw
        gw.SCOPES_DIR = str(self.temp.name)
        server = gw.ThreadingHTTPServer(("127.0.0.1", 0), gw.GatewayHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        base = "http://127.0.0.1:%d" % server.server_address[1]
        rpc = base + "/twirp/github.actions.results.api.v1.CacheService/CreateCacheEntry"

        def post(token):
            req = urllib.request.Request(
                rpc, data=b'{"key":"main-brew","version":"v1"}', method="POST")
            req.add_header("Content-Type", "application/json")
            if token is not None:
                req.add_header("Authorization", "Bearer " + token)
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    return resp.status, json.loads(resp.read())
            except urllib.error.HTTPError as exc:
                return exc.code, json.loads(exc.read())

        status, payload = post("wrong-token")
        self.assertEqual(status, 401)
        self.assertEqual(payload["code"], "unauthenticated")
        status, payload = post(None)
        self.assertEqual(status, 401)
        status, payload = post("good-token")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertIn("signedUploadUrl", payload)
        # Health endpoint needs no auth.
        with urllib.request.urlopen(base + "/healthz", timeout=5) as resp:
            self.assertEqual(resp.status, 200)


if __name__ == "__main__":
    unittest.main()
