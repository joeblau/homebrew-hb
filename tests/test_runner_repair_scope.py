"""Exercise repair scope changes and supervisor updates using temporary plists."""

import json
import plistlib
import shlex
import unittest

import test_runner_repair as repair_fixture


class RunnerRepairScopeTests(unittest.TestCase):
    # Reuse the native-listener fixture without inheriting its test methods.
    setUp = repair_fixture.RunnerRepairTests.setUp
    shell = repair_fixture.RunnerRepairTests.shell

    def supervisor(self, args=None):
        return repair_fixture.RunnerRepairTests.supervisor(self, args)

    def configure_arguments(self):
        return json.loads((self.root / "configure-args.json").read_text())

    def argument(self, name):
        arguments = self.configure_arguments()
        return arguments[arguments.index(name) + 1]

    def test_org_override_uses_destination_url_and_default_group(self):
        result = self.shell(TEST_REPAIR_ORG="lev7finance")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.argument("--url"), "https://github.com/lev7finance")
        self.assertNotIn("--runnergroup", self.configure_arguments())
        settings = json.loads((self.runner / ".runner").read_text())
        self.assertEqual(settings["poolName"], "Default")
        self.assertEqual(settings["agentName"], self.settings["agentName"])

    def test_org_override_preserves_explicit_destination_group(self):
        result = self.shell(TEST_REPAIR_ORG="lev7finance", TEST_RUNNER_GROUP="Destination ARM runners")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.argument("--url"), "https://github.com/lev7finance")
        self.assertEqual(self.argument("--runnergroup"), "Destination ARM runners")

    def test_repo_override_uses_destination_repository_url(self):
        result = self.shell(TEST_REPAIR_REPO="lev7finance/payments")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.argument("--url"), "https://github.com/lev7finance/payments")
        self.assertNotIn("--runnergroup", self.configure_arguments())

    def test_scope_change_leaves_persistent_service_plist_unchanged(self):
        before = self.plist.read_bytes()
        result = self.shell(TEST_REPAIR_ORG="lev7finance")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.plist.read_bytes(), before)
        self.assertEqual((self.root / "started").read_text(), "runner-1\n")

    def test_failed_authentication_plain_retry_keeps_intended_org_and_group(self):
        failure = self.shell(
            TEST_REPAIR_ORG="lev7finance", TEST_RUNNER_GROUP="Destination Macs", TEST_CONFIG_FAIL="1",
        )
        self.assertNotEqual(failure.returncode, 0, failure.stderr)
        self.assertFalse((self.runner / ".runner").exists())
        self.assertFalse((self.root / "started").exists())
        retry = self.shell()
        self.assertEqual(retry.returncode, 0, retry.stderr)
        self.assertEqual(self.argument("--url"), "https://github.com/lev7finance")
        self.assertEqual(self.argument("--runnergroup"), "Destination Macs")
        self.assertEqual((self.root / "listener-calls").read_text(), "remove\nconfigure\nremove\nconfigure\n")

    def test_ephemeral_scope_update_preserves_other_arguments_and_plist_settings(self):
        for scope_args in (("--repo", "acme/source"), ("--repo=acme/source",)):
            with self.subTest(scope_args=scope_args):
                arguments = [
                    "/opt/homebrew/opt/runner-setup/bin/runner-ephemeral",
                    "--dir", str(self.runner), "--name=Mini01-runner-1",
                    "--pat-file", "/private/PAT files/runner.pat", *scope_args,
                    "--labels=old,labels", "--max-failures", "7",
                ]
                self.supervisor(arguments)
                original = plistlib.loads(self.plist.read_bytes())
                result = self.shell(TEST_REPAIR_ORG="lev7finance", TEST_LABELS="macos,finance,arm64")
                self.assertEqual(result.returncode, 0, result.stderr)
                updated = plistlib.loads(self.plist.read_bytes())
                actual = updated.pop("ProgramArguments")
                original.pop("ProgramArguments")
                self.assertEqual(updated, original)
                self.assertEqual(actual, [
                    "/opt/homebrew/opt/runner-setup/bin/runner-ephemeral",
                    "--dir", str(self.runner), "--name=Mini01-runner-1",
                    "--pat-file", "/private/PAT files/runner.pat", "--max-failures", "7",
                    "--url", "https://github.com/lev7finance", "--labels", "macos,finance,arm64",
                ])
                self.assertEqual(self.argument("--url"), "https://github.com/lev7finance")
                self.assertIn("--ephemeral", self.configure_arguments())

    def test_ephemeral_authentication_failure_retry_updates_old_supervisor_scope(self):
        self.supervisor()
        before = self.plist.read_bytes()
        failure = self.shell(TEST_REPAIR_ORG="lev7finance", TEST_CONFIG_FAIL="1")
        self.assertNotEqual(failure.returncode, 0, failure.stderr)
        self.assertEqual(self.plist.read_bytes(), before)
        self.assertFalse((self.root / "runners/.registration-backups/runner-1.pending-plist").exists())
        retry = self.shell()
        self.assertEqual(retry.returncode, 0, retry.stderr)
        arguments = plistlib.loads(self.plist.read_bytes())["ProgramArguments"]
        self.assertEqual(arguments[arguments.index("--url") + 1], "https://github.com/lev7finance")
        self.assertNotIn("--repo", arguments)
        self.assertEqual(self.argument("--url"), "https://github.com/lev7finance")

    def test_pending_plist_retry_reuses_new_registration_without_token_or_local_removal(self):
        self.supervisor()
        before = self.plist.read_bytes()
        failure = self.shell(TEST_REPAIR_ORG="lev7finance", TEST_PLIST_INSTALL_FAIL="1")
        self.assertNotEqual(failure.returncode, 0, failure.stderr)
        self.assertEqual(self.plist.read_bytes(), before)
        pending = self.root / "runners/.registration-backups/runner-1.pending-plist"
        self.assertTrue(pending.is_file())
        registered = (self.runner / ".runner").read_bytes()
        self.assertEqual(json.loads(registered)["gitHubUrl"], "https://github.com/lev7finance")
        calls = (self.root / "listener-calls").read_text()
        self.assertEqual(calls, "remove\nconfigure\n")
        self.assertFalse((self.root / "started").exists())
        retry = self.shell(RUNNER_TOKEN="", TEST_LABELS_SET="0")
        self.assertEqual(retry.returncode, 0, retry.stderr)
        self.assertEqual((self.runner / ".runner").read_bytes(), registered)
        self.assertEqual((self.root / "listener-calls").read_text(), calls)
        self.assertEqual((self.root / "stopped").read_text(), "runner-1\n")
        self.assertEqual((self.root / "started").read_text(), "runner-1\n")
        self.assertFalse(pending.exists())
        arguments = plistlib.loads(self.plist.read_bytes())["ProgramArguments"]
        self.assertEqual(arguments[arguments.index("--url") + 1], "https://github.com/lev7finance")

    def test_pending_plist_does_not_allow_another_scope_change(self):
        self.supervisor()
        failure = self.shell(TEST_REPAIR_ORG="lev7finance", TEST_PLIST_INSTALL_FAIL="1")
        self.assertNotEqual(failure.returncode, 0, failure.stderr)
        before = self.plist.read_bytes()
        retry = self.shell(TEST_REPAIR_ORG="different-org")
        self.assertNotEqual(retry.returncode, 0, retry.stderr)
        self.assertIn("before changing scope again", retry.stderr)
        self.assertEqual(self.plist.read_bytes(), before)
        self.assertEqual((self.root / "listener-calls").read_text(), "remove\nconfigure\n")
        self.assertFalse((self.root / "started").exists())

    def test_local_removal_failure_does_not_strand_an_incomplete_supervisor_update(self):
        self.supervisor()
        original_settings = (self.runner / ".runner").read_bytes()
        original_plist = self.plist.read_bytes()
        failure = self.shell(TEST_REPAIR_ORG="lev7finance", TEST_REMOVE_FAIL="1")
        self.assertNotEqual(failure.returncode, 0, failure.stderr)
        self.assertEqual((self.runner / ".runner").read_bytes(), original_settings)
        self.assertEqual(self.plist.read_bytes(), original_plist)
        self.assertFalse((self.root / "runners/.registration-backups/runner-1.pending-plist").exists())
        retry = self.shell(TEST_REPAIR_ORG="lev7finance")
        self.assertEqual(retry.returncode, 0, retry.stderr)
        self.assertEqual((self.root / "listener-calls").read_text(), "remove\nremove\nconfigure\n")
        self.assertEqual(self.argument("--url"), "https://github.com/lev7finance")

    def test_cross_host_supervisor_api_refuses_github_scope_before_shutdown(self):
        self.supervisor([
            "/opt/homebrew/opt/runner-setup/bin/runner-ephemeral",
            "--dir", str(self.runner), "--repo", "acme/source",
            "--api-url", "https://github.enterprise.invalid/api/v3",
        ])
        result = self.shell(TEST_REPAIR_ORG="lev7finance")
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn("--api-url", result.stderr)
        self.assertFalse((self.root / "stopped").exists())
        self.assertFalse((self.root / "listener-calls").exists())

    def test_scope_parser_rejects_invalid_conflicting_or_unrelated_command_options(self):
        for args in (
            ("repair", "--runner", "1", "--org", "https://github.com/acme"),
            ("repair", "--runner", "1", "--org", "acme/repo"),
            ("repair", "--runner", "1", "--org="),
            ("repair", "--runner", "1", "--repo", "acme"),
            ("repair", "--runner", "1", "--repo", "acme/repo/extra"),
            ("repair", "--runner", "1", "--repo", ""),
            ("repair", "--runner", "1", "--org", "acme", "--repo", "acme/repo"),
            ("run", "--runner", "1", "--org", "acme"),
            ("start", "--runner", "1", "--repo", "acme/repo"),
        ):
            with self.subTest(args=args):
                body = 'REPAIR_LABELS_SET=0; TARGET_RUNNER=""; parse_args ' + shlex.join(args)
                result = self.shell(body)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertFalse((self.root / "stopped").exists())
                self.assertFalse((self.root / "listener-calls").exists())


if __name__ == "__main__":
    unittest.main()
