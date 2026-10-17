# Bootstrap for a GitHub Actions runner Mac. On a fresh Mini:
#
#   brew tap joeblau/hb
#   brew bundle --file="$(brew --repository joeblau/hb)/Brewfile"
#   runner-setup --org ORG_NAME --token REGISTRATION_TOKEN --runners 2 --colima
#
tap "joeblau/hb"
brew "joeblau/hb/runner-setup" # brings docker, jq and python as dependencies
brew "colima"                  # Linux VM for Docker jobs (runner-setup --colima)
brew "docker-buildx"           # buildx plugin wired up by setup-colima
brew "tailscale"               # boot-time remote access; setup starts its system service
