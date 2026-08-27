# Token management for self-hosted runners

`runner-token` mints short-lived GitHub Actions runner tokens on demand, so a
long-lived credential never has to sit in a shell env var, a dotfile, or a
runner directory. It prints **only** the token to stdout (progress to stderr),
which makes it composable:

```bash
RUNNER_TOKEN=$(runner-token registration --org acme) runner-setup --org acme
runner-cleanup --all --yes --token "$(runner-token removal --org acme)"
runner-token audit
```

## Token types and lifetimes

| Token | What it does | Lifetime | How you get one |
| --- | --- | --- | --- |
| Registration token | Lets `config.sh` register a new runner | ~1 hour | `runner-token registration --org ORG` (or `--repo OWNER/REPO`) |
| Removal token | Lets `config.sh remove` deregister a runner | ~1 hour | `runner-token removal --org ORG` (or `--repo OWNER/REPO`) |
| GitHub App installation access token | Authorizes the two calls above | ~1 hour | Minted internally from an RS256 JWT (backend 1) |
| GitHub App JWT | Authorizes minting the installation token | ~9 minutes here (GitHub cap: 10) | Signed locally with `openssl`, never leaves the machine except as the `Authorization` header |
| Runner OAuth credentials (`.credentials`) | Lets a registered runner poll for jobs | Auto-refreshed by the runner itself | Written by `config.sh`; **not** something you manage |

Registration and removal tokens are different objects: a registration token
**cannot** remove a runner and vice versa. Both are single-purpose and expire
in about an hour, which is exactly why they should be fetched seconds before
use rather than stored.

## Authorizing backends (priority order)

`runner-token` needs one *authorizing* credential to mint the short-lived
runner token. It picks the first fully-configured backend:

### 1. GitHub App (preferred)

```bash
export GH_APP_ID=123456
export GH_APP_INSTALLATION_ID=98765432
export GH_APP_KEY_PATH="$HOME/.config/gh-runner-app.pem"   # or GH_APP_KEY with the PEM inline
runner-token registration --org acme
```

The flow, implemented with `openssl` + `curl` only:

1. Build a JWT header `{"alg":"RS256","typ":"JWT"}` and payload
   `{"iat": now-60, "exp": now+540, "iss": GH_APP_ID}`, base64url-encode each.
2. Sign `base64url(header).base64url(payload)` with the app's PEM private key:
   `openssl dgst -sha256 -sign app.pem`, then base64url-encode the signature.
3. `POST /app/installations/{id}/access_tokens` with `Authorization: Bearer <jwt>`
   to get an installation access token (~1h).
4. `POST /orgs/{org}/actions/runners/registration-token` (or `remove-token`,
   or the `/repos/{owner}/{repo}/...` variants) with that installation token.

Least-privilege permissions for the app — grant nothing else:

- **Organization runners:** Organization permissions → **Self-hosted runners: write**
- **Repository runners:** Repository permissions → **Administration: write**

Prefer `GH_APP_KEY_PATH` over `GH_APP_KEY`: multi-line PEM in an env var is
easy to mangle, and a file can be `chmod 600`.

### 2. Fine-grained PAT in an env var

```bash
export GITHUB_PAT=github_pat_...
runner-token registration --org acme
```

Scope the PAT to the single org/repo with the same permissions as above
("Self-hosted runners: write" for orgs, "Administration: write" for repos) and
give it a short expiry. Note the PAT still lives in your shell environment —
readable by any child process you spawn — so backend 1 or 3 is better on
shared machines.

### 3. macOS Keychain

```bash
security add-generic-password -s gh-runner-pat -a "$USER" -w   # prompts for the PAT
runner-token registration --org acme --keychain-item gh-runner-pat
```

The PAT is read with `security find-generic-password -w` at the moment of use
and never sits in the shell environment. You may get a Keychain unlock prompt.

## Why tokens must never be stored in runner dirs

- `/opt/github-runners/runner-N` is read by the runner service account and by
  any job the runner executes. A token parked in a `.env` file there is one
  compromised workflow away from exfiltration.
- LaunchDaemon plists (`/Library/LaunchDaemons/com.github.runner-*.plist`) are
  world-readable by launchd design. Anything under `EnvironmentVariables` is
  visible to every local user.
- Shell history, `ps` output, and CI logs all routinely leak pasted tokens.
  `runner-token` passes credentials to `curl` through a `0600 --config` file in
  a `0700` temp dir precisely so they never appear in `ps`.
- `config.sh` writes `.runner`, `.credentials`, and `.credentials_rsaparams` —
  those are the runner's *own* working credentials and must stay `0600`.
  Everything else secret-shaped in that directory is a mistake.

## Rotation policy

- **Never persist registration/removal tokens.** Mint one per operation; if
  one may have been exposed (pasted in chat, committed, logged), just let it
  expire (~1h) — no revocation needed — and rotate the *authorizing*
  credential if that was exposed instead.
- **GitHub App private key:** rotate at least annually, or immediately on any
  suspicion of exposure. Generate a new key in the app settings, update
  `GH_APP_KEY_PATH`, delete the old key from GitHub.
- **PATs:** use fine-grained PATs with an expiry date (≤ 90 days) scoped to the
  minimum org/repo and permission. Replace on expiry; revoke immediately on
  exposure.
- **Runner OAuth credentials** are rotated by the runner itself; if a runner
  dir is compromised, `runner-cleanup --token "$(runner-token removal ...)"`
  the runner and re-provision.

## Auditing

```bash
runner-token audit
```

Scans `/opt/github-runners` and the `com.github.runner-*.plist` LaunchDaemons
for:

- `ghp_`/`gho_`/`ghu_`/`ghs_`/`ghr_`/`github_pat_` token shapes and PEM
  private-key blocks (paths reported, never the secret values),
- stray `.env` / `*.env` files,
- unexpected `EnvironmentVariables` keys in plists (runner-setup only ever
  writes `HOME` and `ACTIONS_RUNNER_SVC`),
- `.runner` / `.credentials*` files with permissions looser than `0600`.

Exit status is `0` when clean and `1` when findings exist, so it can run in a
cron/CI check.
