# typed: false
# frozen_string_literal: true

class RunnerSetup < Formula
  desc "Provision multiple GitHub Actions self-hosted runners on macOS"
  homepage "https://github.com/joeblau/homebrew-hb"
  url "https://github.com/joeblau/homebrew-hb/archive/refs/tags/v1.0.0.tar.gz"
  sha256 "1170ef75aaa4e95d87e2cc64fd517af921a7e982ea675415ebb4222c1fcadf58"
  license "MIT"

  depends_on :macos

  def install
    bin.install "runner-setup"
  end

  def caveats
    <<~EOS
      Set up self-hosted runners (defaults to 2):

        runner-setup --org ORG_NAME --token REGISTRATION_TOKEN [--runners N]

      Get a REGISTRATION_TOKEN from:
        https://github.com/organizations/ORG_NAME/settings/actions/runners/new

      runner-setup itself does NOT need sudo to install, but at RUNTIME it uses
      sudo to create /opt/github-runners and to install system LaunchDaemons.
      The runner processes run as your (non-root) user, not as root, because the
      GitHub Actions runner refuses to run as root.

      SECURITY: a token passed via --token is visible in `ps` to other local
      users while the command runs. On shared machines prefer:

        RUNNER_TOKEN=xxxxx runner-setup --org ORG_NAME

      Tokens are short-lived; rotate any token that may have been exposed.
    EOS
  end

  test do
    assert_match "USAGE", shell_output("#{bin}/runner-setup --help")
    # Missing required args must fail with a nonzero exit and a clear message.
    # Clear RUNNER_TOKEN so the test is hermetic: the script falls back to that
    # env var for --token, so a tester/CI with it exported would otherwise pass
    # validation and never reach the exit-2 path.
    output = shell_output("RUNNER_TOKEN= #{bin}/runner-setup --org acme 2>&1", 2)
    assert_match "Missing required argument", output
  end
end
