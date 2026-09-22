"""runner-agent Slack notifications: webhook from env or a 0600 file, the URL
travels via a curl -K config (never argv), delivery is best-effort. Fake curl,
no network, no Slack."""

import json
from pathlib import Path
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_runner_agent import RunnerAgentBase, SCRIPT  # noqa: E402


WEBHOOK = "https://hooks.slack.com/services/T00/B00/secret-token"

# Records argv, saves a copy of each -K config file, and appends each stdin
# payload as one JSON line. FAKE_CURL_FAIL simulates an unreachable webhook.
FAKE_CURL = r'''#!/bin/bash
printf '%s\n' "curl $*" >> "$FAKE_LOG"
while [[ $# -gt 0 ]]; do
  if [[ "$1" == "-K" ]]; then cat "$2" >> "$FAKE_CURL_CONF"; shift 2; else shift; fi
done
cat >> "$FAKE_CURL_BODY"
printf '\n' >> "$FAKE_CURL_BODY"
[[ -z "${FAKE_CURL_FAIL:-}" ]] || exit 7
'''


class SlackTests(RunnerAgentBase):
    def setUp(self):
        super().setUp()
        self.write_exe("curl", FAKE_CURL)
        self.bodies = self.root / "slack-bodies.jsonl"
        self.confs = self.root / "slack-conf.txt"

    def slack_env(self, event_name, event, **extra):
        env = self.env(event_name, event,
                       SLACK_WEBHOOK_URL=WEBHOOK,
                       FAKE_CURL_BODY=str(self.bodies),
                       FAKE_CURL_CONF=str(self.confs))
        env.update(extra)
        return env

    def slack_messages(self):
        if not self.bodies.exists():
            return []
        return [json.loads(line)["text"]
                for line in self.bodies.read_text().splitlines() if line.strip()]

    def notify(self, *args, env=None, success=True):
        result = subprocess.run(
            ["/bin/bash", str(SCRIPT), "notify", *args], cwd=self.work,
            env=env or self.slack_env("workflow_run", {}),
            text=True, capture_output=True, timeout=20,
        )
        if success:
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stderr)
        return result

    def test_pr_outcome_posts_message_with_pr_link(self):
        env = self.slack_env("workflow_run", self.workflow_run_event(),
                             FAKE_CLAUDE_EDIT=str(self.work / "src.txt"))
        self.agent("gate", env)
        self.agent("run", env)
        self.agent("publish", env)
        [message] = self.slack_messages()
        self.assertIn("opened a fix PR", message)
        self.assertIn("CI run 4242 in acme/app", message)
        self.assertIn("<https://github.com/acme/app/pull/77|view>", message)
        # Identifiers only: no agent summary, logs, or secrets in the payload.
        self.assertNotIn("Root cause", message)
        self.assertNotIn("test-key", message)
        self.assertNotIn("hooks.slack.com", message)

    def test_diagnosis_without_fix_posts_comment_message(self):
        env = self.slack_env("workflow_run", self.workflow_run_event(pull_requests=[]))
        self.agent("gate", env)
        self.agent("run", env)
        self.agent("publish", env)
        [message] = self.slack_messages()
        self.assertIn("no code change", message)
        self.assertIn("<https://github.com/acme/app/actions/runs/4242|view>", message)
        self.assertEqual(self.outputs_dict()["outcome"], "comment")

    def test_mention_acknowledged_at_gate_and_completed_at_publish(self):
        env = self.slack_env("issue_comment", self.comment_event(),
                             FAKE_CLAUDE_EDIT=str(self.work / "src.txt"))
        self.agent("gate", env)
        [ack] = self.slack_messages()
        self.assertIn("acknowledged @runner-agent on acme/app #12", ack)
        self.agent("run", env)
        self.agent("publish", env)
        ack, done = self.slack_messages()
        self.assertIn("pushed a fix", done)
        self.assertIn("@runner-agent request on acme/app #12", done)
        self.assertIn("<https://github.com/acme/app/pull/12|view>", done)

    def test_webhook_url_travels_via_curl_config_never_argv(self):
        env = self.slack_env("workflow_run", self.workflow_run_event(),
                             FAKE_CLAUDE_EDIT=str(self.work / "src.txt"))
        self.agent("gate", env)
        self.agent("run", env)
        self.agent("publish", env)
        curl_calls = [c for c in self.calls() if c.startswith("curl ")]
        self.assertTrue(curl_calls)
        for call in curl_calls:
            self.assertIn("-K ", call)
            self.assertNotIn("hooks.slack.com", call)
        self.assertIn(f'url = "{WEBHOOK}"', self.confs.read_text())

    def test_no_webhook_configured_skips_notification(self):
        env = self.env("workflow_run", self.workflow_run_event(),
                       FAKE_CLAUDE_EDIT=str(self.work / "src.txt"),
                       FAKE_CURL_BODY=str(self.bodies),
                       FAKE_CURL_CONF=str(self.confs))
        self.agent("gate", env)
        self.agent("run", env)
        result = self.agent("publish", env)
        self.assertEqual(self.outputs_dict()["outcome"], "pr")
        self.assertEqual(self.slack_messages(), [])
        self.assertFalse(any(c.startswith("curl ") for c in self.calls()))
        self.assertIn("No Slack webhook configured", result.stderr)

    def test_failed_delivery_never_fails_the_job(self):
        env = self.slack_env("workflow_run", self.workflow_run_event(),
                             FAKE_CLAUDE_EDIT=str(self.work / "src.txt"),
                             FAKE_CURL_FAIL="1")
        self.agent("gate", env)
        self.agent("run", env)
        result = self.agent("publish", env)
        self.assertEqual(self.outputs_dict()["outcome"], "pr")
        self.assertIn("Slack notification failed (curl rc=7)", result.stderr)

    def test_webhook_url_from_0600_config_file(self):
        webhook_file = self.root / "slack-webhook"
        webhook_file.write_text(f"{WEBHOOK}\n")
        webhook_file.chmod(0o600)
        env = self.env("workflow_run", {},
                       RUNNER_AGENT_SLACK_WEBHOOK_FILE=str(webhook_file),
                       FAKE_CURL_BODY=str(self.bodies),
                       FAKE_CURL_CONF=str(self.confs))
        self.assertNotIn("SLACK_WEBHOOK_URL", env)
        self.notify("gate green on acme/app", env=env)
        self.assertEqual(self.slack_messages(), ["gate green on acme/app"])
        self.assertIn(f'url = "{WEBHOOK}"', self.confs.read_text())

    def test_permissive_webhook_file_warns_but_still_sends(self):
        webhook_file = self.root / "slack-webhook"
        webhook_file.write_text(f"{WEBHOOK}\n")
        webhook_file.chmod(0o644)
        env = self.env("workflow_run", {},
                       RUNNER_AGENT_SLACK_WEBHOOK_FILE=str(webhook_file),
                       FAKE_CURL_BODY=str(self.bodies),
                       FAKE_CURL_CONF=str(self.confs))
        result = self.notify("hello", env=env)
        self.assertIn("chmod 600", result.stderr)
        self.assertEqual(self.slack_messages(), ["hello"])

    def test_notify_subcommand_posts_text_with_optional_link(self):
        self.notify("deploy finished", "https://example.com/runs/1")
        self.assertEqual(self.slack_messages(),
                         ["deploy finished — <https://example.com/runs/1|link>"])

    def test_notify_requires_a_message(self):
        result = self.notify(success=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn("notify requires a message", result.stderr)


if __name__ == "__main__":
    unittest.main()
