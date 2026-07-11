//! agent-mcp: a generic MCP server that wraps any agent CLI.
//!
//! Running `agent-mcp <agent>` starts a stdio MCP server that exposes a single
//! tool, `ask_<agent>`. Each call to that tool runs the wrapped agent CLI
//! non-interactively in the current project directory and returns its output —
//! so any MCP client (Claude Code, Codex, …) can delegate work to that agent.

mod adapter;
mod config;
mod mcp;
mod runner;

use std::path::PathBuf;

use anyhow::{Context, Result};
use clap::Parser;

use adapter::Adapter;
use config::Layered;

/// Generic MCP server that wraps any agent CLI (codex, claude, gemini, grok, …).
#[derive(Parser, Debug)]
#[command(name = "agent-mcp", version, about, long_about = None)]
struct Cli {
    /// Agent to wrap: a built-in (codex, claude, gemini, grok) or any command
    /// on your PATH. Unknown names are run as `<name> "<prompt>"`.
    #[arg(value_name = "AGENT")]
    agent: Option<String>,

    /// Merge an extra TOML config file on top of the built-ins.
    #[arg(long, value_name = "PATH")]
    config: Option<PathBuf>,

    /// Directory the wrapped agent runs in (default: current directory).
    #[arg(long, value_name = "DIR")]
    cwd: Option<PathBuf>,

    /// Override the per-call timeout, in seconds.
    #[arg(long, value_name = "SECS")]
    timeout: Option<u64>,

    /// Override the exposed MCP tool name (default: ask_<agent>).
    #[arg(long, value_name = "NAME")]
    tool_name: Option<String>,

    /// List available agent adapters and exit.
    #[arg(long)]
    list: bool,
}

fn main() {
    if let Err(e) = run() {
        eprintln!("agent-mcp: error: {e:#}");
        std::process::exit(1);
    }
}

fn run() -> Result<()> {
    let cli = Cli::parse();

    let cwd = match cli.cwd {
        Some(dir) => dir,
        None => std::env::current_dir().context("determining current directory")?,
    };
    let cwd = cwd
        .canonicalize()
        .with_context(|| format!("resolving cwd {}", cwd.display()))?;

    let layered = Layered::build(&cwd, cli.config.as_deref())?;

    if cli.list {
        println!("Available agent adapters:");
        for name in layered.names() {
            let a = Adapter::resolve(&name, layered.for_agent(&name), "<cwd>");
            println!("  {:<10} {}", name, a.invocation_hint());
        }
        println!(
            "\nAny other name works too and runs as `<name> \"<prompt>\"`.\n\
             Override or add agents in ~/.config/agent-mcp/config.toml or ./.agent-mcp.toml."
        );
        return Ok(());
    }

    let agent = match cli.agent {
        Some(a) => a,
        None => {
            anyhow::bail!(
                "no agent specified. Try `agent-mcp codex`, or `agent-mcp --list`."
            );
        }
    };

    let cwd_display = cwd.display().to_string();
    let mut adapter = Adapter::resolve(&agent, layered.for_agent(&agent), &cwd_display);
    if let Some(t) = cli.timeout {
        adapter.timeout_secs = t;
    }
    if let Some(name) = cli.tool_name {
        adapter.tool_name = name;
    }

    // All diagnostics go to stderr — stdout is reserved for the JSON-RPC stream.
    if !layered.is_known(&agent) {
        eprintln!(
            "agent-mcp: '{agent}' is not a built-in adapter; wrapping it generically."
        );
    }
    eprintln!(
        "agent-mcp v{} serving tool `{}` -> `{}` (cwd: {})",
        env!("CARGO_PKG_VERSION"),
        adapter.tool_name,
        adapter.invocation_hint(),
        cwd_display,
    );

    mcp::Server::new(adapter, cwd).serve()
}
