# typed: false
# frozen_string_literal: true

class RunnerSetup < Formula
  desc "Provision and tear down GitHub Actions self-hosted runners on macOS"
  homepage "https://github.com/joeblau/homebrew-hb"
  url "https://github.com/joeblau/homebrew-hb/archive/refs/tags/v1.18.2.tar.gz"
  sha256 "b292d2b924d2749513692eb32bdf25d188f9bfa49fa8cd5e6c8a442e497c55a0"
  license "MIT"

  depends_on "docker"
  depends_on "jq"
  depends_on :macos
  depends_on "python@3.14"

  def install
    bin.install %w[
      runner-setup
      runner-bootstrap
      runner-cleanup
      runner-ephemeral
      runner-prune
      runner-ramscratch
      runner-health
      runner-cache
      runner-logs
      runner-bench
      runner-upgrade
      runner-autoscale
      runner-token
      runner-netisolate
      runner-docker-builder
      runner-bazel-cache
      runner-sccache
      runner-git-mirror
      runner-tailscale-guard
      runner-mcp
      runner-agent
    ].map { |tool| "bin/#{tool}" }
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

      Prepare the Mac itself first — Homebrew, the tap's Brewfile, power
      settings, FileVault off, Remote Login, tailscaled at boot with its
      kernel-panic reboot guard, automatic login, a cleared Dock:
        runner-bootstrap               # --check verifies without changing
      runner-bootstrap clears the Dock and desktop directly. Use
      --skip-clear-desktop to keep the existing desktop.
      runner-setup only provisions runners. It upgrades itself through
      Homebrew first (--skip-self-upgrade to skip), resumes runners left in
      maintenance, and finishes only when every runner is listening. The
      Colima boot daemon supervises the VM and restarts it if macOS raced it
      past Rosetta at boot.

      Services run as system LaunchDaemons with a non-root service user.
      Runners restart after any exit, including a clean exit. Setup reruns
      repair existing startup settings and remove leftover runner login agents.
      A loaded service is not proof that a runner is ready. Verify startup:
        runner-health --once --dry-run
      Test a canary reboot before GUI or SSH login; confirm runners are online
      in GitHub. Boot verification guide:
        https://github.com/joeblau/homebrew-hb/blob/main/docs/boot-startup.md

      Enable Colima at boot and wait for Docker before accepting jobs:
        runner-setup --org ORG_NAME --runners N --colima
      Existing same-scope runners need no new registration token. Enable this
      between jobs; preserve your existing scope and runner count.

      On Apple Silicon, Colima setup installs missing Rosetta 2, accepting
      Apple's license. linux/amd64 containers run through Rosetta, never QEMU.
      Runners stay offline until Rosetta is active. For an existing VZ VM,
      pause dispatch and drain jobs, then rerun setup:
        runner-setup --org ORG_NAME --runners N --colima
      Explicit --colima setup stops the idle VM and restarts it under system
      launchd so it survives logout. Keep your existing scope and runner count.

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
    %w[
      runner-setup
      runner-bootstrap
      runner-cleanup
      runner-ephemeral
      runner-prune
      runner-ramscratch
      runner-health
      runner-cache
      runner-logs
      runner-bench
      runner-upgrade
      runner-autoscale
      runner-token
      runner-netisolate
      runner-docker-builder
      runner-bazel-cache
      runner-sccache
      runner-git-mirror
      runner-tailscale-guard
      runner-mcp
      runner-agent
    ].each do |tool|
      assert_match "USAGE", shell_output("#{bin}/#{tool} --help 2>&1")
    end

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
