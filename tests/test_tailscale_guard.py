"""runner-tailscale-guard regression tests (tailscale/tailscale#18953).

macOS leaks one kernel ifnet reference per UDP packet tailscaled sends and
panics when the 32-bit count wraps. The guard estimates the leak from
tailscaled's magicsock_send_udp* counters — accumulated across tailscaled
restarts, reset per kernel boot session — and reboots idle-first before the
wrap. These tests run the real script against fake tailscale/sysctl/pgrep/
id/shutdown binaries; no real socket, root, or reboot is involved.
"""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
BIN = REPO / "bin"
GUARD = BIN / "runner-tailscale-guard"

FAKES = {
    # Counter lives in $TEST_ROOT/metrics as raw `name value` lines.
    'tailscale': '''echo "$*" >> "$TEST_ROOT/tailscale-calls"
cat "$TEST_ROOT/metrics" 2>/dev/null || exit 1''',
    'sysctl': 'cat "$TEST_ROOT/boot-uuid" 2>/dev/null || echo BOOT-A',
    # A file $TEST_ROOT/busy marks a Runner.Worker mid-job.
    'pgrep': '[[ -f "$TEST_ROOT/busy" ]]',
    'id': 'echo "${FAKE_UID:-0}"',
    'shutdown': 'echo "$*" >> "$TEST_ROOT/shutdown-calls"',
}


class TailscaleGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        for name, body in FAKES.items():
            path = bin_dir / name
            path.write_text("#!/bin/bash\n" + body + "\n")
            path.chmod(0o755)
        self.socket = self.root / "tailscaled.socket"
        self.socket.write_text("")
        self.state_dir = self.root / "state"

    def run_guard(self, *args, **env):
        return subprocess.run(
            [str(GUARD), *args],
            env=dict(os.environ, TEST_ROOT=str(self.root),
                     RUNNER_TS_GUARD_SOCKET=str(self.socket),
                     RUNNER_TS_GUARD_DIR=str(self.state_dir),
                     PATH=f'{self.root / "bin"}:{os.environ["PATH"]}', **env),
            text=True, capture_output=True)

    def set_counter(self, ipv4, ipv6=0):
        (self.root / "metrics").write_text(
            f"magicsock_send_udp {ipv4}\n"
            f"magicsock_send_udp_ipv6 {ipv6}\n"
            "magicsock_send_derp 12345\n")

    def shutdown_calls(self):
        path = self.root / "shutdown-calls"
        return path.read_text().splitlines() if path.exists() else []

    def test_help_prints_usage(self):
        result = self.run_guard("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("USAGE", result.stderr)

    def test_unreachable_daemon_is_a_clean_noop(self):
        self.socket.unlink()
        result = self.run_guard("check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("nothing to guard", result.stderr)
        self.assertEqual(self.shutdown_calls(), [])

    def test_below_soft_limit_saves_state_and_does_not_reboot(self):
        self.set_counter(1_000_000)
        result = self.run_guard("check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("below the soft limit", result.stderr)
        self.assertEqual(self.shutdown_calls(), [])
        state = (self.state_dir / "state").read_text()
        self.assertIn("boot=BOOT-A", state)
        self.assertIn("last=1000000", state)

    def test_counters_sum_udp_lines_only(self):
        self.set_counter(600, ipv6=400)
        result = self.run_guard("status", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        doc = json.loads(result.stdout)
        self.assertEqual(doc["counter"], 1000)  # derp line excluded

    def test_estimate_survives_tailscaled_restart(self):
        # First pass: 1.5e9 sent. Daemon restarts (counter falls to 1.0e9);
        # the kernel kept the first 1.5e9, so the estimate must be 2.5e9 and
        # cross the 2.0e9 soft limit.
        self.set_counter(1_500_000_000)
        self.run_guard("check")
        self.set_counter(1_000_000_000)
        result = self.run_guard("check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("2500000000", result.stderr)
        self.assertTrue(self.shutdown_calls())

    def test_reboot_resets_the_estimate(self):
        self.set_counter(3_000_000_000)
        self.run_guard("check", "--dry-run")
        (self.root / "boot-uuid").write_text("BOOT-B\n")
        self.set_counter(5)
        result = self.run_guard("check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("below the soft limit", result.stderr)
        self.assertEqual(self.shutdown_calls(), [])

    def test_soft_limit_defers_while_a_job_runs(self):
        (self.root / "busy").write_text("")
        self.set_counter(2_500_000_000)
        result = self.run_guard("check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("deferring the reboot", result.stderr)
        self.assertEqual(self.shutdown_calls(), [])

    def test_hard_limit_reboots_even_mid_job(self):
        (self.root / "busy").write_text("")
        self.set_counter(3_900_000_000)
        result = self.run_guard("check")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.shutdown_calls()
        self.assertEqual(len(calls), 1)
        self.assertIn("-r now", calls[0])

    def test_idle_soft_limit_reboots(self):
        self.set_counter(2_000_000_001)
        result = self.run_guard("check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.shutdown_calls())

    def test_dry_run_never_reboots(self):
        self.set_counter(4_000_000_000)
        result = self.run_guard("check", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Would reboot", result.stderr)
        self.assertEqual(self.shutdown_calls(), [])

    def test_non_root_reports_instead_of_rebooting(self):
        self.set_counter(4_000_000_000)
        result = self.run_guard("check", FAKE_UID="501")
        self.assertEqual(result.returncode, 1)
        self.assertIn("not root", result.stderr)
        self.assertEqual(self.shutdown_calls(), [])

    def test_custom_limits_are_validated(self):
        result = self.run_guard("check", "--soft-limit", "100", "--hard-limit", "50")
        self.assertEqual(result.returncode, 2)
        self.assertIn("must not exceed", result.stderr)

    def test_status_json_when_unreachable(self):
        self.socket.unlink()
        result = self.run_guard("status", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        doc = json.loads(result.stdout)
        self.assertFalse(doc["reachable"])


if __name__ == "__main__":
    unittest.main()
