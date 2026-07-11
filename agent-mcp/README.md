# agent-mcp

A generic [MCP](https://modelcontextprotocol.io) server, written in Rust, that
wraps **any agent CLI** and exposes it as a tool. Run it from a project
directory and every other agent — Claude Code, Codex, Cursor, … — can delegate
work to the wrapped agent, executed right there in that project.

```
brew install joeblau/hb/agent-mcp
agent-mcp codex        # serve `ask_codex` over stdio for the current project
```

## How it works

`agent-mcp <agent>` starts a stdio MCP server (newline-delimited JSON-RPC 2.0)
that advertises a single tool, `ask_<agent>`. When a client calls that tool with
a `prompt`, agent-mcp:

1. runs the wrapped agent CLI **non-interactively** in the working directory,
2. captures its stdout (and stderr on failure), and
3. returns it as the tool result.

Diagnostics go to stderr; stdout carries only the protocol stream. There are no
ports and no daemon — the MCP client launches `agent-mcp` as a subprocess, which
is exactly how MCP stdio servers are meant to run.

## Built-in agents

| Agent    | Invocation            |
| -------- | --------------------- |
| `codex`  | `codex exec {prompt}` |
| `claude` | `claude -p {prompt}`  |
| `gemini` | `gemini -p {prompt}`  |
| `grok`   | `grok -p {prompt}`    |

Any other name works too and is wrapped generically as `<name> "<prompt>"`, so
`agent-mcp mytool` runs `mytool "<prompt>"`. List everything with:

```
agent-mcp --list
```

The wrapped CLI must be installed, on `PATH`, and (usually) authenticated.

## Registering with a client

Claude Code:

```
claude mcp add codex -- agent-mcp codex
```

Any client that launches stdio MCP servers:

```json
{
  "mcpServers": {
    "codex": { "command": "agent-mcp", "args": ["codex"] }
  }
}
```

The client sets the subprocess working directory, which becomes the project the
wrapped agent operates in.

## CLI options

```
agent-mcp <AGENT> [OPTIONS]

  --config <PATH>     Merge an extra TOML config file on top of the built-ins
  --cwd <DIR>         Directory the wrapped agent runs in (default: current dir)
  --timeout <SECS>    Per-call timeout in seconds (default: 300)
  --tool-name <NAME>  Override the exposed tool name (default: ask_<agent>)
  --list              List available agent adapters and exit
```

## Configuration

Adapters are resolved by layering, later layers winning:

1. built-in defaults
2. `~/.config/agent-mcp/config.toml` (or `$XDG_CONFIG_HOME/agent-mcp/config.toml`)
3. project-local `./.agent-mcp.toml`
4. an explicit `--config <path>`
5. CLI flag overrides (`--cwd`, `--timeout`, `--tool-name`)

Each `[agents.<name>]` table may set any subset of these fields; unset fields
fall back to the layer beneath.

```toml
[agents.codex]
command     = "codex"          # binary to run
args        = ["exec", "{prompt}"]  # {prompt} is substituted; if absent and
                               #   prompt_via = "arg", the prompt is appended
prompt_via  = "arg"            # "arg" (default) or "stdin"
timeout_secs = 600             # default 300
tool_name   = "ask_codex"      # default ask_<name>
description = "Delegate to Codex."

[agents.codex.env]             # extra environment variables for the child
CODEX_QUIET = "1"

# Add a brand-new agent that reads its prompt from stdin:
[agents.local-llm]
command    = "ollama"
args       = ["run", "llama3"]
prompt_via = "stdin"
```

### Notes

- `{prompt}` may appear inside any argument and is replaced with the prompt
  text. With `prompt_via = "arg"` and no `{prompt}` placeholder, the prompt is
  appended as the final argument. With `prompt_via = "stdin"` it is piped to the
  child's stdin.
- For real delegation you often want the wrapped agent's auto-approval mode
  (e.g. Gemini `--yolo`, Grok `--always-approve`) so it can act without
  prompting. Add those to `args`.

## Building from source

```
cargo build --release
./target/release/agent-mcp --list
```
