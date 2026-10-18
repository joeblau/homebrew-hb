# Everything a GitHub Actions runner Mac needs. runner-bootstrap installs it;
# on a fresh Mac (no Homebrew yet):
#
#   /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/joeblau/homebrew-hb/main/bin/runner-bootstrap)"
#   runner-setup --org ORG_NAME --token REGISTRATION_TOKEN --runners 2 --colima
#
tap "joeblau/hb"
brew "joeblau/hb/runner-setup" # runner-setup, runner-bootstrap and tools; brings docker, jq, python
brew "joeblau/hb/clear-mac"    # clears the Dock and desktop widgets
brew "gnu-tar"                 # GNU tar, available as gtar
brew "gh"                      # GitHub CLI
brew "gnupg"                   # OpenPGP tools, including gpg
brew "colima"                  # Linux VM for Docker jobs (runner-setup --colima)
brew "docker-buildx"           # buildx plugin wired up by setup-colima
brew "tailscale"               # boot-time remote access; bootstrap starts its system service
