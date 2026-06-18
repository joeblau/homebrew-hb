# typed: false
# frozen_string_literal: true

class RunnerSetup < Formula
  desc "Provision and tear down GitHub Actions self-hosted runners on macOS"
  homepage "https://github.com/joeblau/homebrew-hb"
  url "https://github.com/joeblau/homebrew-hb/archive/refs/tags/v1.2.0.tar.gz"
  sha256 "5456d159d89a443f48a77d06a2114279c81dbd9e5b321952c92981b22b602ccd"
  license "MIT"

  depends_on :macos

  def install
    bin.install "runner-setup"
    bin.install "runner-cleanup"
  end

  def caveats
    <<~EOS
      Set up self-hosted runners (defaults to 2). Pick the scope that matches
      where you generated the token — an org token and a repo token are NOT
      interchangeable (using the wrong one returns HTTP 404):

        # Organization runner:
        runner-setup --org ORG_NAME --token REGISTRATION_TOKEN [--runners N]

        # Repository runner:
        runner-setup --repo OWNER/REPO --token REGISTRATION_TOKEN [--runners N]

      Get a REGISTRATION_TOKEN (and the exact --url) from the "New runner" page:
        Org:  https://github.com/organizations/ORG_NAME/settings/actions/runners/new
        Repo: https://github.com/OWNER/REPO/settings/actions/runners/new

      To share one runner across MULTIPLE repos, register at the org level
      (--org) and grant repo access under Org Settings > Actions > Runner groups.
      A --repo runner is bound to that single repository.

      Runners register as <machine-name>-runner-N by default, so several Macs
      can join one scope without name clashes. Override with --name-prefix.

      Remove runners later (use a *removal* token to also deregister on GitHub):
        runner-cleanup --all --token REMOVE_TOKEN
        runner-cleanup --runner 2

      Neither command needs sudo to install, but at RUNTIME they use sudo to
      manage /opt/github-runners and system LaunchDaemons. The runner processes
      run as your (non-root) user, because the GitHub Actions runner refuses to
      run as root.

      SECURITY: a token passed via --token is visible in `ps` to other local
      users while the command runs. On shared machines prefer the env var:

        RUNNER_TOKEN=xxxxx runner-setup --repo OWNER/REPO
        RUNNER_REMOVE_TOKEN=xxxxx runner-cleanup --all

      Tokens are short-lived; rotate any token that may have been exposed.
    EOS
  end

  test do
    assert_match "USAGE", shell_output("#{bin}/runner-setup --help")
    assert_match "USAGE", shell_output("#{bin}/runner-cleanup --help")

    # No registration scope chosen must fail with exit 2 and a clear message.
    no_scope = shell_output("RUNNER_TOKEN= #{bin}/runner-setup --token x 2>&1", 2)
    assert_match "exactly one of --org", no_scope

    # Scope present but token missing must also fail with exit 2. Clear
    # RUNNER_TOKEN so the test is hermetic: the script falls back to that env var
    # for --token, so a tester/CI with it exported would otherwise pass
    # validation and never reach the exit-2 path.
    no_token = shell_output("RUNNER_TOKEN= #{bin}/runner-setup --org acme 2>&1", 2)
    assert_match "Missing required argument", no_token
  end
end
