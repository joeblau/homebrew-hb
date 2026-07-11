# typed: false
# frozen_string_literal: true

class AgentMcp < Formula
  desc "Generic MCP server that wraps any agent CLI (codex, claude, gemini, grok)"
  homepage "https://github.com/joeblau/homebrew-hb"
  url "https://github.com/joeblau/homebrew-hb/archive/refs/tags/v1.4.0.tar.gz"
  sha256 "acdc852aa0ced8e313500740e9b58be830d7b31c34b6045ea4be2cd4b3c1c9cb"
  license "MIT"

  depends_on "rust" => :build

  def install
    cd "agent-mcp" do
      system "cargo", "install", *std_cargo_args
    end
  end

  def caveats
    <<~EOS
      agent-mcp starts a stdio MCP server that wraps an agent CLI. Run it from a
      project directory; each tool call runs the wrapped agent there and returns
      its output, so any MCP client can delegate work to that agent.

      Built-in agents: codex, claude, gemini, grok. Any other name is wrapped
      generically as `<name> "<prompt>"`. List them with:

        agent-mcp --list

      Register it with an MCP client (example: Claude Code) so `ask_codex` is
      available inside a project:

        claude mcp add codex -- agent-mcp codex

      Or add it to a client config that launches stdio servers, e.g.:

        {
          "mcpServers": {
            "codex": { "command": "agent-mcp", "args": ["codex"] }
          }
        }

      To instead start a long-lived server in its own window that several
      clients connect to, use HTTP daemon mode:

        agent-mcp codex --http                  # binds 127.0.0.1:8787
        claude mcp add --transport http codex http://127.0.0.1:8787/

      Override or add agents (command, args, timeout, stdin/arg delivery) in
      ~/.config/agent-mcp/config.toml or a project-local ./.agent-mcp.toml. See
      the README for the config schema.

      The wrapped agent must be installed and on PATH, and typically needs its
      own auth configured. For headless delegation you may also want to enable
      that agent's auto-approval mode via config `args`.
    EOS
  end

  test do
    assert_match "Usage", shell_output("#{bin}/agent-mcp --help")
    assert_match "codex", shell_output("#{bin}/agent-mcp --list")

    # A generic agent wrapping `echo` must answer a full MCP tools/call round
    # trip over stdio with the echoed prompt.
    require "open3"
    require "json"
    requests = [
      { jsonrpc: "2.0", id: 1, method: "initialize",
        params: { protocolVersion: "2025-06-18", capabilities: {},
                  clientInfo: { name: "brew-test", version: "0" } } },
      { jsonrpc: "2.0", id: 2, method: "tools/call",
        params: { name: "ask_echo", arguments: { prompt: "brew-ok" } } },
    ].map { |r| "#{JSON.dump(r)}\n" }.join

    out, = Open3.capture2(bin/"agent-mcp", "echo", stdin_data: requests)
    assert_match "brew-ok", out
  end
end
