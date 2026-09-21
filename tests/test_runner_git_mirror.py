"""Regression tests for runner-git-mirror; no network, launchd, sudo, or /opt.

Real local git repositories are created in tempdirs (git is local-only here);
sudo/launchctl are replaced by shell-function stubs in a prelude that sources
the script, following tests/test_runner_cache.py.
"""

from pathlib import Path
import json
import os
import re
import shlex
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "runner-git-mirror"


class RunnerGitMirrorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="runner-git-mirror-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for directory in (
            "git/mirrors", "git/config/repos", "git/config/tokens", "git/state",
            "git/gitconfig", "git/bin", "git/logs", "runners", "home", "origins",
        ):
            (self.root / directory).mkdir(parents=True, exist_ok=True)

    def shell(self, body):
        prelude = r'''
source "$1"
TEST_ROOT="$2"
GIT_ROOT="${TEST_ROOT}/git"
MIRROR_ROOT="${GIT_ROOT}/mirrors"
CONFIG_DIR="${GIT_ROOT}/config"
REPOS_DIR="${CONFIG_DIR}/repos"
TOKENS_DIR="${CONFIG_DIR}/tokens"
STATE_DIR="${GIT_ROOT}/state"
GITCONFIG_DIR="${GIT_ROOT}/gitconfig"
BIN_DIR="${GIT_ROOT}/bin"
ASKPASS_WRAPPER="${BIN_DIR}/git-mirror-askpass.sh"
LOG_DIR="${GIT_ROOT}/logs"
RUNNER_ROOT="${TEST_ROOT}/runners"
PLIST_DEST="${TEST_ROOT}/com.github.runner-git-mirror.plist"
detect_platform() { :; }
detect_user() {
  REAL_USER="$(whoami)"; REAL_GROUP="$(id -gn)"; REAL_UID="$(id -u)"
  REAL_HOME="${TEST_ROOT}/home"
}
require_cmds() { :; }
ensure_sudo() { :; }
as_user() { "$@"; }
sudo() {
  if [[ "$1" == "-n" ]]; then shift; fi
  case "$1" in
    launchctl)
      printf '%s\n' "$*" >> "${TEST_ROOT}/launchctl.log"
      [[ "$2" == "print" ]] && return 1
      return 0 ;;
    chown) return 0 ;;
    *) "$@" ;;
  esac
}
'''
        return subprocess.run(
            ["/bin/bash", "-c", prelude + "\n" + body, "test", str(SCRIPT), str(self.root)],
            text=True, capture_output=True, check=False,
        )

    def git(self, *args, cwd=None):
        return subprocess.run(
            ["git", "-c", "user.email=t@example.com", "-c", "user.name=Test", *args],
            cwd=cwd, text=True, capture_output=True, check=True,
        ).stdout.strip()

    def make_origin(self, name, branch="main"):
        origin = self.root / "origins" / name
        origin.mkdir(parents=True, exist_ok=True)
        self.git("init", "-q", "-b", branch, cwd=origin)
        (origin / "file.txt").write_text("v1\n")
        self.git("add", "file.txt", cwd=origin)
        self.git("commit", "-q", "-m", "initial", cwd=origin)
        return origin

    def commit_to_origin(self, origin, filename, content, message):
        (origin / filename).write_text(content)
        self.git("add", filename, cwd=origin)
        self.git("commit", "-q", "-m", message, cwd=origin)

    def add_mirror(self, repo="acme/proj", url=None):
        # Canonical URLs use file:// so git applies url.insteadOf rewrites at
        # transport time (plain local paths bypass transport and are stat'ed
        # by git clone before any rewrite, unlike https/ssh in production).
        url = url or "file://" + str(self.make_origin("proj"))
        result = self.shell(
            "parse_args add --repo %s --url %s; cmd_add" % (shlex.quote(repo), shlex.quote(url))
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return url

    def mirror_path(self, slug="acme--proj"):
        return self.root / "git/mirrors" / (slug + ".git")

    # ------------------------------------------------------------------
    # help / argument validation
    # ------------------------------------------------------------------
    def test_help_prints_usage_block(self):
        result = self.shell("main --help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("USAGE:", result.stderr)

    def test_usage_errors_exit_2_before_any_work(self):
        bad = [
            "frobnicate",
            "add",
            "add --repo acme/proj --url",
            "add --repo acme/proj --token-file",
            "install-git-config",
            "install-git-config --runner 1 --all",
            "install-git-config --runner notanumber",
        ]
        for args in bad:
            with self.subTest(args=args):
                tokens = shlex.split(args)
                result = self.shell("parse_args " + " ".join(shlex.quote(t) for t in tokens))
                self.assertEqual(result.returncode, 2, result.stderr)

    def test_repo_and_url_injection_is_rejected_as_data(self):
        bad = [
            "add --repo acme/$(touch escaped)",
            "add --repo ../bad/path --url https://example.com/x.git",
            "add --repo acme/proj --url 'has a space'",
            "add --repo acme/proj --url -C",
        ]
        for args in bad:
            with self.subTest(args=args):
                tokens = shlex.split(args)
                result = self.shell("parse_args " + " ".join(shlex.quote(t) for t in tokens))
                self.assertEqual(result.returncode, 2, result.stderr)
        self.assertFalse((self.root / "escaped").exists())

    # ------------------------------------------------------------------
    # add / list / remove
    # ------------------------------------------------------------------
    def test_add_clones_bare_mirror_records_config_and_installs_timer(self):
        url = self.add_mirror()
        mirror = self.mirror_path()
        self.assertTrue(mirror.is_dir())
        self.assertEqual(self.git("--git-dir", str(mirror), "rev-parse", "--is-bare-repository"), "true")
        self.assertEqual(
            self.git("--git-dir", str(mirror), "log", "-1", "--format=%s", "main"), "initial"
        )
        conf = (self.root / "git/config/repos/acme--proj.env").read_text()
        self.assertIn('REPO="acme/proj"', conf)
        self.assertIn('URL="%s"' % url, conf)
        state = (self.root / "git/state/acme--proj.state").read_text()
        self.assertIn('LAST_FETCH_STATUS="ok"', state)
        self.assertIn('FETCH_COUNT="1"', state)
        # Refresh timer installed with an interval and bootstrapped by launchd.
        plist = (self.root / "com.github.runner-git-mirror.plist").read_text()
        self.assertIn("<key>StartInterval</key>", plist)
        self.assertIn("<string>update</string>", plist)
        self.assertIn("com.github.runner-git-mirror", plist)
        launchctl = (self.root / "launchctl.log").read_text()
        self.assertIn("launchctl bootstrap system", launchctl)

    def test_add_refuses_duplicate_mirror(self):
        self.add_mirror()
        url = "file://" + str(self.make_origin("proj2"))
        result = self.shell(
            "parse_args add --repo acme/proj --url %s; cmd_add" % shlex.quote(url)
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("already mirrored", result.stderr)

    def test_list_empty_then_populated(self):
        result = self.shell("parse_args list; cmd_list")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("No mirrors installed", result.stdout)
        url = self.add_mirror()
        result = self.shell("parse_args list; cmd_list")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("acme/proj", result.stdout)
        self.assertIn(url, result.stdout)

    def test_remove_deletes_mirror_config_state_and_last_timer(self):
        self.add_mirror()
        result = self.shell("parse_args remove --repo acme/proj; cmd_remove")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.mirror_path().exists())
        self.assertFalse((self.root / "git/config/repos/acme--proj.env").exists())
        self.assertFalse((self.root / "git/state/acme--proj.state").exists())
        # Last mirror removed: timer plist deleted and daemon booted out.
        self.assertFalse((self.root / "com.github.runner-git-mirror.plist").exists())
        self.assertIn("launchctl bootout system/com.github.runner-git-mirror",
                      (self.root / "launchctl.log").read_text())
        result = self.shell("parse_args remove --repo acme/proj; cmd_remove")
        self.assertEqual(result.returncode, 1)
        self.assertIn("not mirrored", result.stderr)

    # ------------------------------------------------------------------
    # update
    # ------------------------------------------------------------------
    def test_update_fetches_new_commits_prunes_and_records_state(self):
        origin = self.make_origin("proj")
        self.add_mirror(url="file://" + str(origin))
        mirror = str(self.mirror_path())

        self.commit_to_origin(origin, "second.txt", "v2\n", "second")
        self.git("branch", "tmp", cwd=origin)
        result = self.shell("parse_args update; cmd_update")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.git("--git-dir", mirror, "log", "-1", "--format=%s", "main"), "second")
        present = subprocess.run(
            ["git", "--git-dir", mirror, "rev-parse", "--verify", "refs/heads/tmp"],
            capture_output=True,
        )
        self.assertEqual(present.returncode, 0, "new upstream branch must reach the mirror")

        self.git("branch", "-D", "tmp", cwd=origin)
        self.commit_to_origin(origin, "third.txt", "v3\n", "third")
        result = self.shell("parse_args update --repo acme/proj; cmd_update")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.git("--git-dir", mirror, "log", "-1", "--format=%s", "main"), "third")
        gone = subprocess.run(
            ["git", "--git-dir", mirror, "rev-parse", "--verify", "refs/heads/tmp"],
            capture_output=True,
        )
        self.assertNotEqual(gone.returncode, 0, "pruned branch must disappear from the mirror")

        state = (self.root / "git/state/acme--proj.state").read_text()
        self.assertIn('LAST_FETCH_STATUS="ok"', state)
        self.assertIn('FETCH_COUNT="3"', state)  # clone + two updates
        self.assertIn('FAIL_COUNT="0"', state)

    def test_update_unknown_repo_fails(self):
        result = self.shell("parse_args update --repo acme/ghost; cmd_update")
        self.assertEqual(result.returncode, 1)
        self.assertIn("not mirrored", result.stderr)

    def test_update_lock_blocks_live_holder_and_reclaims_stale(self):
        origin = self.make_origin("proj")
        self.add_mirror(url="file://" + str(origin))
        self.commit_to_origin(origin, "second.txt", "v2\n", "second")
        # A live PID holds the lock: the update is skipped and reported.
        result = self.shell(r'''
parse_args update; mkdir "${MIRROR_ROOT}/acme--proj.git.lock"
printf '%s\n' "$$" > "${MIRROR_ROOT}/acme--proj.git.lock/pid"
cmd_update
''')
        self.assertEqual(result.returncode, 1)
        self.assertIn("in flight", result.stderr)
        # A dead PID's lock is reclaimed and the fetch proceeds.
        result = self.shell(r'''
parse_args update
rm -rf "${MIRROR_ROOT}/acme--proj.git.lock"
sleep 0.1 & dead=$!; wait "${dead}" 2>/dev/null || true
mkdir "${MIRROR_ROOT}/acme--proj.git.lock"
printf '%s\n' "${dead}" > "${MIRROR_ROOT}/acme--proj.git.lock/pid"
cmd_update
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("stale lock", result.stderr)
        mirror = str(self.mirror_path())
        self.assertEqual(self.git("--git-dir", mirror, "log", "-1", "--format=%s", "main"), "second")
        self.assertFalse(Path(mirror + ".lock").exists(), "lock must be released after update")

    def test_update_isolated_from_installed_url_rewrites(self):
        origin = self.make_origin("proj")
        self.add_mirror(url="file://" + str(origin))
        self.commit_to_origin(origin, "second.txt", "v2\n", "second")
        # A poisoned ambient gitconfig rewrites the canonical URL to a dead
        # path. update must override GIT_CONFIG_GLOBAL/GIT_CONFIG_NOSYSTEM so
        # the mirror's own fetch still reaches the real remote.
        poison = self.root / "poison.gitconfig"
        poison.write_text('[url "%s"]\n\tinsteadOf = %s\n' % (self.root / "dead", "file://" + str(origin)))
        result = self.shell(
            'export GIT_CONFIG_GLOBAL=%s; parse_args update; cmd_update' % shlex.quote(str(poison))
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        mirror = str(self.mirror_path())
        self.assertEqual(self.git("--git-dir", mirror, "log", "-1", "--format=%s", "main"), "second")

    def test_failed_fetch_keeps_last_good_mirror_and_counts_failure(self):
        origin = self.make_origin("proj")
        self.add_mirror(url="file://" + str(origin))
        # Break the upstream: the mirror must stay usable at its last state.
        offline = self.root / "origins/proj-offline"
        origin.rename(offline)
        result = self.shell("parse_args update; cmd_update")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Fetch failed", result.stderr)
        mirror = str(self.mirror_path())
        self.assertEqual(self.git("--git-dir", mirror, "log", "-1", "--format=%s", "main"), "initial")
        state = (self.root / "git/state/acme--proj.state").read_text()
        self.assertIn('LAST_FETCH_STATUS="failed"', state)
        self.assertIn('FAIL_COUNT="1"', state)

    # ------------------------------------------------------------------
    # install-git-config / uninstall-git-config
    # ------------------------------------------------------------------
    def provision_runner(self, n):
        (self.root / "runners" / ("runner-%d" % n)).mkdir(parents=True, exist_ok=True)

    def test_gitconfig_rewrite_clones_from_mirror_and_keeps_canonical_origin(self):
        origin = self.make_origin("proj")
        url = "file://" + str(origin)
        self.add_mirror(url=url)
        self.provision_runner(1)
        result = self.shell("parse_args install-git-config --runner 1; cmd_install_git_config")
        self.assertEqual(result.returncode, 0, result.stderr)

        gitconfig = self.root / "git/gitconfig/runner-1.gitconfig"
        text = gitconfig.read_text()
        mirror = str(self.mirror_path())
        self.assertIn('[url "%s"]' % mirror, text)
        self.assertIn("insteadOf = %s" % url, text)
        self.assertIn("insteadOf = %s.git" % url, text)
        # Reverse repair rule maps the mirror path back to the canonical URL.
        self.assertIn('[url "%s.git"]' % url, text)
        self.assertIn("insteadOf = %s" % mirror, text)

        env = (self.root / "runners/runner-1/.env").read_text()
        self.assertIn("GIT_CONFIG_GLOBAL=%s" % gitconfig, env)

        # The canonical URL is dead: the clone only succeeds via the mirror.
        backup = self.root / "origins/proj-offline"
        origin.rename(backup)
        dest = self.root / "checkout"
        env_vars = dict(os.environ, GIT_CONFIG_GLOBAL=str(gitconfig))
        clone = subprocess.run(
            ["git", "clone", url, str(dest)], env=env_vars, text=True, capture_output=True
        )
        self.assertEqual(clone.returncode, 0, clone.stderr)
        self.assertEqual((dest / "file.txt").read_text(), "v1\n")
        # Origin URL in the checkout stays canonical — no repair step needed.
        self.assertEqual(self.git("-C", str(dest), "remote", "get-url", "origin"), url)

    def test_unmirrored_repositories_fall_through_to_network(self):
        self.add_mirror()
        self.provision_runner(1)
        result = self.shell("parse_args install-git-config --runner 1; cmd_install_git_config")
        self.assertEqual(result.returncode, 0, result.stderr)
        gitconfig = str(self.root / "git/gitconfig/runner-1.gitconfig")
        other = self.make_origin("other")
        dest = self.root / "checkout-other"
        env_vars = dict(os.environ, GIT_CONFIG_GLOBAL=gitconfig)
        clone = subprocess.run(
            ["git", "clone", str(other), str(dest)], env=env_vars, text=True, capture_output=True
        )
        self.assertEqual(clone.returncode, 0, clone.stderr)
        self.assertEqual(self.git("-C", str(dest), "remote", "get-url", "origin"), str(other))

    def test_install_git_config_is_idempotent_and_uninstall_reverts(self):
        self.add_mirror()
        self.provision_runner(1)
        for _ in range(2):
            result = self.shell("parse_args install-git-config --runner 1; cmd_install_git_config")
            self.assertEqual(result.returncode, 0, result.stderr)
        env_file = self.root / "runners/runner-1/.env"
        lines = [l for l in env_file.read_text().splitlines() if l.startswith("GIT_CONFIG_GLOBAL=")]
        self.assertEqual(len(lines), 1, ".env wiring must be idempotent")

        result = self.shell("parse_args uninstall-git-config --runner 1; cmd_uninstall_git_config")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("GIT_CONFIG_GLOBAL", env_file.read_text())
        self.assertFalse((self.root / "git/gitconfig/runner-1.gitconfig").exists())

    def test_install_git_config_requires_provisioned_runner_and_mirrors(self):
        result = self.shell("parse_args install-git-config --runner 7; cmd_install_git_config")
        self.assertEqual(result.returncode, 1)
        self.assertIn("No mirrors installed", result.stderr)
        self.add_mirror()
        result = self.shell("parse_args install-git-config --runner 7; cmd_install_git_config")
        self.assertEqual(result.returncode, 1)
        self.assertIn("not provisioned", result.stderr)

    def test_install_all_and_add_refreshes_installed_gitconfigs(self):
        self.add_mirror()
        self.provision_runner(1)
        self.provision_runner(2)
        result = self.shell("parse_args install-git-config --all; cmd_install_git_config")
        self.assertEqual(result.returncode, 0, result.stderr)
        for n in (1, 2):
            self.assertTrue((self.root / "git/gitconfig" / ("runner-%d.gitconfig" % n)).exists())
        # Adding a second mirror regenerates installed gitconfigs.
        other = self.make_origin("other")
        self.add_mirror(repo="acme/other", url="file://" + str(other))
        text = (self.root / "git/gitconfig/runner-1.gitconfig").read_text()
        self.assertIn("acme--other.git", text)

    def test_existing_user_gitconfig_is_included(self):
        (self.root / "home/.gitconfig").write_text("[user]\n\tname = Operator\n")
        self.add_mirror()
        self.provision_runner(1)
        result = self.shell("parse_args install-git-config --runner 1; cmd_install_git_config")
        self.assertEqual(result.returncode, 0, result.stderr)
        text = (self.root / "git/gitconfig/runner-1.gitconfig").read_text()
        self.assertIn("path = %s/.gitconfig" % (self.root / "home"), text)

    # ------------------------------------------------------------------
    # credentials
    # ------------------------------------------------------------------
    def test_token_file_is_copied_0600_and_never_on_argv_or_in_outputs(self):
        origin = self.make_origin("proj")
        token = self.root / "pat.txt"
        token.write_text("ghp_mirrorsecret123\n")
        result = self.shell(
            "parse_args add --repo acme/proj --url %s --token-file %s; cmd_add"
            % (shlex.quote(str(origin)), shlex.quote(str(token)))
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        stored = self.root / "git/config/tokens/acme--proj"
        self.assertEqual(stored.read_text().strip(), "ghp_mirrorsecret123")
        self.assertEqual(oct(stored.stat().st_mode & 0o777), "0o600")
        for artifact in (
            self.root / "git/config/repos/acme--proj.env",
            self.root / "com.github.runner-git-mirror.plist",
            self.root / "git/bin/git-mirror-askpass.sh",
        ):
            self.assertNotIn("ghp_mirrorsecret123", artifact.read_text())
        self.assertNotIn("ghp_mirrorsecret123", result.stdout + result.stderr)
        # The askpass wrapper answers username/password prompts from the file.
        result = self.shell(
            'MIRROR_TOKEN_FILE="${TOKENS_DIR}/acme--proj" "${ASKPASS_WRAPPER}" "Username for x:"; '
            'MIRROR_TOKEN_FILE="${TOKENS_DIR}/acme--proj" "${ASKPASS_WRAPPER}" "Password for x:"'
        )
        self.assertEqual(result.stdout.splitlines(), ["x-access-token", "ghp_mirrorsecret123"])

    # ------------------------------------------------------------------
    # status / metrics
    # ------------------------------------------------------------------
    def test_status_reports_freshness_sizes_and_coverage(self):
        self.add_mirror()
        self.provision_runner(1)
        result = self.shell("parse_args status; cmd_status")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("acme/proj", result.stdout)
        self.assertIn("last fetch:", result.stdout)
        self.assertIn("ok", result.stdout)
        self.assertIn("mirror:", result.stdout)

        result = self.shell("parse_args status --json; cmd_status")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data["daemon"], "com.github.runner-git-mirror")
        self.assertEqual(data["runners_configured"], 0)
        self.assertEqual(data["runners_detected"], 1)
        self.assertEqual(len(data["mirrors"]), 1)
        mirror = data["mirrors"][0]
        self.assertEqual(mirror["repo"], "acme/proj")
        self.assertEqual(mirror["last_fetch_status"], "ok")
        self.assertGreater(mirror["size_kb"], 0)
        self.assertGreaterEqual(mirror["age_s"], 0)

        result = self.shell("parse_args install-git-config --runner 1; cmd_install_git_config")
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.shell("parse_args status --json; cmd_status")
        self.assertEqual(json.loads(result.stdout)["runners_configured"], 1)

    def test_metrics_reports_counters_without_fabricating_hit_rate(self):
        self.add_mirror()
        self.provision_runner(1)
        self.provision_runner(2)
        result = self.shell("parse_args metrics; cmd_metrics")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Mirrors installed:", result.stdout)
        self.assertIn("Fetch operations:", result.stdout)
        self.assertIn("Git-config coverage:", result.stdout)
        self.assertIn("0 of 2 runners wired", result.stdout)
        self.assertIn("unavailable", result.stdout)
        self.assertNotIn("%", result.stdout)


if __name__ == "__main__":
    unittest.main()
