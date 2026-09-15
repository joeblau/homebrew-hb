"""runner-agent gate/run/publish against event fixtures, a fake claude, and a
real bare git remote with a fake gh. No network, no Anthropic, no GitHub."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "runner-agent"

# Multi-line bodies are folded onto one log line so calls() stays one-per-call.
FAKE_GH = r'''#!/bin/bash
printf '%s' "gh $*" | tr '\n' ' ' >> "$FAKE_LOG"; printf '\n' >> "$FAKE_LOG"
case "$1 $2" in
  "pr view")   printf '%s\n' "${FAKE_PR_VIEW:-{\"headRefName\":\"feature\",\"isCrossRepository\":false}}" ;;
  "pr create") echo "https://github.com/acme/app/pull/77" ;;
  *) : ;;
esac
'''

# Fake claude: records the prompt it was given and returns a canned envelope.
FAKE_CLAUDE = r'''#!/bin/bash
printf '%s\n' "claude $*" >> "$FAKE_LOG"
cat > "$FAKE_PROMPT_OUT"
if [[ -n "${FAKE_CLAUDE_EDIT:-}" ]]; then printf 'fixed\n' > "$FAKE_CLAUDE_EDIT"; fi
if [[ -n "${FAKE_CLAUDE_WORKFLOW_EDIT:-}" ]]; then mkdir -p .github/workflows; printf 'evil\n' > .github/workflows/x.yml; mkdir -p __pycache__; printf 'x' > __pycache__/a.pyc; fi
if [[ "${FAKE_CLAUDE_MODE:-ok}" == "error" ]]; then
  echo '{"type":"result","subtype":"error_max_turns","is_error":true,"result":"gave up","num_turns":60,"total_cost_usd":1.5}'
  exit 1
fi
echo '{"type":"result","subtype":"success","is_error":false,"num_turns":7,"total_cost_usd":0.42,"result":"Title: Fix flaky date parsing in build\n\n### Root cause\nTimezone assumption.\n\n### Change\nUse UTC.\n\n### Verification\nmake test passed.\n\n### Runner notes\nNone"}'
'''


class RunnerAgentBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="runner-agent-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.write_exe("gh", FAKE_GH)
        self.write_exe("claude", FAKE_CLAUDE)
        # runner-mcp is resolved next to the script; a symlinked bin dir keeps it real.
        (self.bin / "runner-mcp").symlink_to(REPO / "runner-mcp")
        (self.bin / "runner-agent").symlink_to(SCRIPT)
        self.state = self.root / "temp"
        self.state.mkdir()
        self.log = self.root / "calls.log"
        self.outputs = self.root / "github_output"
        self.git_init_remote_and_clone()

    def write_exe(self, name, body):
        path = self.bin / name
        path.write_text(body)
        path.chmod(0o755)

    def git(self, *args, cwd=None):
        return subprocess.run(["git", *args], cwd=cwd or self.work, text=True, capture_output=True, check=True).stdout

    def git_init_remote_and_clone(self):
        self.remote = self.root / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", "--initial-branch=main", str(self.remote)], check=True)
        self.work = self.root / "work"
        subprocess.run(["git", "clone", "-q", str(self.remote), str(self.work)], check=True, capture_output=True)
        self.git("config", "user.name", "t")
        self.git("config", "user.email", "t@example.com")
        (self.work / "README.md").write_text("hello\n")
        (self.work / "src.txt").write_text("broken\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "init")
        self.git("push", "-q", "origin", "main")
        self.git("checkout", "-q", "-b", "feature")
        (self.work / "src.txt").write_text("feature\n")
        self.git("commit", "-q", "-am", "feature work")
        self.git("push", "-q", "-u", "origin", "feature")
        self.sha = self.git("rev-parse", "HEAD").strip()

    def env(self, event_name, event, **extra):
        path = self.root / "event.json"
        path.write_text(json.dumps(event))
        env = {
            "PATH": f"{self.bin}:/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin",
            "HOME": str(self.root),
            "RUNNER_TEMP": str(self.state),
            "GITHUB_EVENT_NAME": event_name,
            "GITHUB_EVENT_PATH": str(path),
            "GITHUB_REPOSITORY": "acme/app",
            "GITHUB_WORKFLOW": "runner-agent",
            "GITHUB_OUTPUT": str(self.outputs),
            "GITHUB_RUN_ID": "999",
            "FAKE_LOG": str(self.log),
            "FAKE_PROMPT_OUT": str(self.root / "prompt.seen"),
            "ANTHROPIC_API_KEY": "test-key",
        }
        env.update(extra)
        return env

    def agent(self, command, env, success=True):
        result = subprocess.run(
            ["/bin/bash", str(SCRIPT), command], cwd=self.work, env=env,
            text=True, capture_output=True, timeout=60,
        )
        if success:
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stderr)
        return result

    def context(self):
        return json.loads((self.state / "runner-agent/context.json").read_text())

    def outputs_dict(self):
        if not self.outputs.exists():
            return {}
        return dict(line.split("=", 1) for line in self.outputs.read_text().splitlines() if "=" in line)

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def workflow_run_event(self, **overrides):
        event = {"workflow_run": {
            "id": 4242, "name": "ci", "conclusion": "failure", "head_branch": "feature",
            "head_sha": self.sha, "html_url": "https://github.com/acme/app/actions/runs/4242",
            "head_repository": {"full_name": "acme/app"},
            "triggering_actor": {"login": "dev"}, "actor": {"login": "dev"},
            "pull_requests": [{"number": 12}],
        }}
        event["workflow_run"].update(overrides)
        return event

    def comment_event(self, body="@runner-agent please fix the typo", pr=True, **overrides):
        issue = {"number": 12, "title": "Typo", "pull_request": {"url": "x"} if pr else None}
        if not pr:
            issue.pop("pull_request")
        event = {"comment": {"id": 555, "body": body, "user": {"type": "User"}, "author_association": "MEMBER"},
                 "issue": issue, "repository": {"default_branch": "main"}}
        event["comment"].update(overrides)
        return event


class GateTests(RunnerAgentBase):
    def test_failed_run_on_same_repo_branch_becomes_ci_fix(self):
        self.agent("gate", self.env("workflow_run", self.workflow_run_event()))
        ctx = self.context()
        self.assertEqual(ctx["mode"], "ci-fix")
        self.assertEqual(ctx["ref"], self.sha)
        self.assertEqual(ctx["base_branch"], "feature")
        self.assertEqual(ctx["work_branch"], "agent/run-4242")
        self.assertEqual(ctx["push_mode"], "new-branch")
        self.assertEqual(ctx["number"], 12)
        self.assertEqual(ctx["run_id"], 4242)
        self.assertIn("run 4242 failed", ctx["task"])
        self.assertEqual(self.outputs_dict()["skip"], "false")
        self.assertEqual(self.outputs_dict()["ref"], self.sha)

    def test_runs_that_must_not_trigger_are_skipped(self):
        cases = {
            "success": self.workflow_run_event(conclusion="success"),
            "fork": self.workflow_run_event(head_repository={"full_name": "someone/app"}),
            "agent branch": self.workflow_run_event(head_branch="agent/run-1"),
            "bot": self.workflow_run_event(triggering_actor={"login": "dependabot[bot]"}),
            "self": self.workflow_run_event(name="runner-agent"),
        }
        for label, event in cases.items():
            with self.subTest(label):
                self.outputs.unlink(missing_ok=True)
                self.agent("gate", self.env("workflow_run", event))
                self.assertTrue(self.context()["skip"], label)
                self.assertEqual(self.outputs_dict()["skip"], "true")

    def test_run_is_attempted_only_once(self):
        self.git("push", "-q", "origin", "HEAD:refs/heads/agent/run-4242")
        self.agent("gate", self.env("workflow_run", self.workflow_run_event()))
        self.assertTrue(self.context()["skip"])
        self.assertIn("already attempted", self.context()["reason"])

    def test_pr_comment_targets_the_pr_branch(self):
        self.agent("gate", self.env("issue_comment", self.comment_event()))
        ctx = self.context()
        self.assertEqual(ctx["mode"], "pr-comment")
        self.assertEqual(ctx["ref"], "feature")
        self.assertEqual(ctx["work_branch"], "feature")
        self.assertEqual(ctx["push_mode"], "existing")
        self.assertEqual(ctx["comment_id"], 555)
        self.assertIn("please fix the typo", ctx["task"])
        self.assertNotIn("@runner-agent", ctx["task"])
        self.assertTrue(any(c.startswith("gh pr view 12") for c in self.calls()))

    def test_issue_comment_opens_new_branch_from_default(self):
        self.agent("gate", self.env("issue_comment", self.comment_event(pr=False)))
        ctx = self.context()
        self.assertEqual(ctx["mode"], "issue-comment")
        self.assertEqual(ctx["ref"], "main")
        self.assertEqual(ctx["work_branch"], "agent/issue-12-555")
        self.assertEqual(ctx["push_mode"], "new-branch")

    def test_review_comment_carries_file_and_hunk(self):
        event = {"comment": {"id": 9, "body": "@runner-agent rename this", "user": {"type": "User"},
                             "author_association": "OWNER", "path": "src.txt", "diff_hunk": "@@ -1 +1 @@\n-broken\n+feature"},
                 "pull_request": {"number": 12, "head": {"ref": "feature", "repo": {"full_name": "acme/app"}}}}
        self.agent("gate", self.env("pull_request_review_comment", event))
        ctx = self.context()
        self.assertEqual(ctx["mode"], "pr-comment")
        self.assertEqual(ctx["path"], "src.txt")
        self.assertIn("+feature", ctx["task"])

    def test_comments_that_must_not_trigger_are_skipped(self):
        cases = {
            "no mention": self.comment_event(body="looks good"),
            "bot": self.comment_event(user={"type": "Bot"}),
            "outsider": self.comment_event(author_association="NONE"),
            "contributor": self.comment_event(author_association="CONTRIBUTOR"),
        }
        for label, event in cases.items():
            with self.subTest(label):
                self.agent("gate", self.env("issue_comment", event))
                self.assertTrue(self.context()["skip"], label)
        fork = self.env("issue_comment", self.comment_event(),
                        FAKE_PR_VIEW='{"headRefName":"feature","isCrossRepository":true}')
        self.agent("gate", fork)
        self.assertIn("fork", self.context()["reason"])

    def test_unsupported_event_is_skipped_not_failed(self):
        self.agent("gate", self.env("push", {}))
        self.assertTrue(self.context()["skip"])


class RunTests(RunnerAgentBase):
    def test_run_builds_ci_fix_prompt_and_records_summary(self):
        env = self.env("workflow_run", self.workflow_run_event(), RUNNER_AGENT_INSTRUCTIONS="Run make test.")
        self.agent("gate", env)
        self.agent("run", env)
        prompt = (self.root / "prompt.seen").read_text()
        self.assertIn("run_id 4242", prompt)
        self.assertIn("gh_failed_run_log", prompt)
        self.assertIn("Run make test.", prompt)
        self.assertIn("Do not run git commit", prompt)
        claude_call = next(c for c in self.calls() if c.startswith("claude "))
        for flag in ("-p", "--output-format json", "--strict-mcp-config", "--max-turns 60", "--max-budget-usd 5",
                     "--allowedTools Read,Edit,Write,Glob,Grep,Bash,mcp__runner__*"):
            self.assertIn(flag, claude_call)
        mcp = json.loads((self.state / "runner-agent/mcp.json").read_text())
        self.assertTrue(mcp["mcpServers"]["runner"]["command"].endswith("runner-mcp"))
        summary = (self.state / "runner-agent/summary.md").read_text()
        self.assertTrue(summary.startswith("Title: Fix flaky date parsing"))
        self.assertEqual(self.outputs_dict()["cost_usd"], "0.42")

    def test_run_requires_credentials_and_surfaces_agent_errors(self):
        env = self.env("workflow_run", self.workflow_run_event())
        self.agent("gate", env)
        no_creds = {k: v for k, v in env.items() if k != "ANTHROPIC_API_KEY"}
        no_creds["GITHUB_ACTIONS"] = "true"
        self.assertIn("ANTHROPIC_API_KEY", self.agent("run", no_creds, success=False).stderr)
        failed = self.agent("run", dict(env, FAKE_CLAUDE_MODE="error"), success=False)
        self.assertIn("error_max_turns", failed.stderr)

    def test_run_refuses_after_skip(self):
        env = self.env("workflow_run", self.workflow_run_event(conclusion="success"))
        self.agent("gate", env)
        self.agent("run", env, success=False)
        self.assertFalse(any(c.startswith("claude") for c in self.calls()))


class PublishTests(RunnerAgentBase):
    def run_pipeline(self, event_name, event, **extra):
        env = self.env(event_name, event, **extra)
        self.agent("gate", env)
        self.agent("run", env)
        return env

    def test_ci_fix_with_changes_opens_pr_and_comments(self):
        env = self.run_pipeline("workflow_run", self.workflow_run_event(), FAKE_CLAUDE_EDIT=str(self.work / "src.txt"))
        self.agent("publish", env)
        self.assertEqual(self.outputs_dict()["outcome"], "pr")
        self.assertEqual(self.outputs_dict()["pr_url"], "https://github.com/acme/app/pull/77")
        branches = self.git("ls-remote", "--heads", "origin")
        self.assertIn("refs/heads/agent/run-4242", branches)
        subject = self.git("log", "-1", "--format=%s", "origin/agent/run-4242")
        self.assertEqual(subject.strip(), "Fix flaky date parsing in build")
        create = next(c for c in self.calls() if c.startswith("gh pr create"))
        self.assertIn("--base feature --head agent/run-4242", create)
        self.assertIn("<!-- runner-agent run=4242 -->", create)
        self.assertNotIn("Title:", create.split("--body", 1)[1])
        self.assertTrue(any(c.startswith("gh pr comment 12") for c in self.calls()))

    def test_ci_fix_without_changes_only_comments(self):
        env = self.run_pipeline("workflow_run", self.workflow_run_event(pull_requests=[]))
        self.agent("publish", env)
        self.assertEqual(self.outputs_dict()["outcome"], "comment")
        self.assertNotIn("refs/heads/agent/run-4242", self.git("ls-remote", "--heads", "origin"))
        commit_comment = next(c for c in self.calls() if c.startswith("gh api"))
        self.assertIn(f"repos/acme/app/commits/{self.sha}/comments", commit_comment)
        self.assertFalse(any(c.startswith("gh pr create") for c in self.calls()))

    def test_pr_comment_pushes_to_the_pr_branch(self):
        env = self.run_pipeline("issue_comment", self.comment_event(), FAKE_CLAUDE_EDIT=str(self.work / "src.txt"))
        before = self.git("rev-parse", "origin/feature").strip()
        self.agent("publish", env)
        self.assertEqual(self.outputs_dict()["outcome"], "pushed")
        after = self.git("rev-parse", "origin/feature").strip()
        self.assertNotEqual(before, after)
        self.assertFalse(any(c.startswith("gh pr create") for c in self.calls()))
        comment = next(c for c in self.calls() if c.startswith("gh pr comment 12"))
        self.assertIn("<!-- runner-agent comment=555 -->", comment)

    def test_workflow_edits_and_build_junk_are_never_published(self):
        env = self.run_pipeline("workflow_run", self.workflow_run_event(),
                                FAKE_CLAUDE_EDIT=str(self.work / "src.txt"), FAKE_CLAUDE_WORKFLOW_EDIT="1")
        self.agent("publish", env)
        files = self.git("ls-tree", "-r", "--name-only", "origin/agent/run-4242")
        self.assertNotIn(".github/workflows/x.yml", files)
        self.assertNotIn("__pycache__", files)
        self.assertIn("src.txt", files)

    def test_snippet_and_help(self):
        snippet = subprocess.run(["/bin/bash", str(SCRIPT), "snippet"], text=True, capture_output=True, timeout=20)
        self.assertEqual(snippet.returncode, 0)
        self.assertIn("uses: joeblau/homebrew-hb/.github/workflows/runner-agent.yml", snippet.stdout)
        self.assertIn("workflow_run", snippet.stdout)
        helptext = subprocess.run(["/bin/bash", str(SCRIPT), "--help"], text=True, capture_output=True, timeout=20)
        self.assertIn("USAGE", helptext.stderr)


if __name__ == "__main__":
    unittest.main()
