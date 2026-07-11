//! Agent adapters: how to invoke a wrapped agent CLI non-interactively.
//!
//! An [`Adapter`] is the fully-resolved recipe used at runtime. It is built by
//! layering, in order of increasing precedence:
//!   1. the built-in defaults ([`builtins`]),
//!   2. the user config      (`~/.config/agent-mcp/config.toml`),
//!   3. the project config    (`./.agent-mcp.toml`),
//!   4. an explicit `--config` file,
//!   5. CLI flag overrides (`--cwd`, `--timeout`, `--tool-name`).

use std::collections::HashMap;

use serde::Deserialize;

/// How the prompt is delivered to the wrapped process.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PromptVia {
    /// Passed as a command-line argument (default). If any arg contains the
    /// `{prompt}` placeholder it is substituted there; otherwise the prompt is
    /// appended as the final argument.
    Arg,
    /// Written to the child's stdin.
    Stdin,
}

impl PromptVia {
    fn parse(s: &str) -> Option<Self> {
        match s.to_ascii_lowercase().as_str() {
            "arg" | "argument" => Some(Self::Arg),
            "stdin" => Some(Self::Stdin),
            _ => None,
        }
    }
}

/// The partial, deserializable form of an adapter as it appears in a config
/// file. Every field is optional so layers can override individually.
#[derive(Debug, Clone, Default, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AdapterConfig {
    pub command: Option<String>,
    pub args: Option<Vec<String>>,
    pub prompt_via: Option<String>,
    pub timeout_secs: Option<u64>,
    pub tool_name: Option<String>,
    pub description: Option<String>,
    pub env: Option<HashMap<String, String>>,
}

impl AdapterConfig {
    /// Merge `over` on top of `self`; set fields in `over` win. Env maps are
    /// unioned (keys in `over` win).
    pub fn merged(self, over: AdapterConfig) -> AdapterConfig {
        AdapterConfig {
            command: over.command.or(self.command),
            args: over.args.or(self.args),
            prompt_via: over.prompt_via.or(self.prompt_via),
            timeout_secs: over.timeout_secs.or(self.timeout_secs),
            tool_name: over.tool_name.or(self.tool_name),
            description: over.description.or(self.description),
            env: match (self.env, over.env) {
                (Some(mut base), Some(more)) => {
                    base.extend(more);
                    Some(base)
                }
                (base, more) => more.or(base),
            },
        }
    }
}

/// A fully-resolved adapter ready to run.
#[derive(Debug, Clone)]
pub struct Adapter {
    pub command: String,
    pub args: Vec<String>,
    pub prompt_via: PromptVia,
    pub timeout_secs: u64,
    pub tool_name: String,
    pub description: String,
    pub env: HashMap<String, String>,
}

impl Adapter {
    /// Resolve the requested `agent` into a runnable adapter, applying the
    /// merged config layer for that agent (may be empty for a generic agent).
    pub fn resolve(agent: &str, cfg: AdapterConfig, cwd_display: &str) -> Adapter {
        let command = cfg.command.unwrap_or_else(|| agent.to_string());
        let prompt_via = cfg
            .prompt_via
            .as_deref()
            .and_then(PromptVia::parse)
            .unwrap_or(PromptVia::Arg);
        // Default args depend on delivery mode: for arg mode, `{command}
        // {prompt}`; for stdin mode, no args (the prompt is piped in).
        let args = cfg.args.unwrap_or_else(|| match prompt_via {
            PromptVia::Arg => vec!["{prompt}".to_string()],
            PromptVia::Stdin => Vec::new(),
        });
        let timeout_secs = cfg.timeout_secs.unwrap_or(300);
        let tool_name = cfg.tool_name.unwrap_or_else(|| default_tool_name(agent));
        let description = cfg.description.unwrap_or_else(|| {
            format!(
                "Delegate a task to the '{agent}' agent CLI. It runs \
                 non-interactively in {cwd_display} and returns its output. \
                 Provide a clear, self-contained prompt."
            )
        });
        Adapter {
            command,
            args,
            prompt_via,
            timeout_secs,
            tool_name,
            description,
            env: cfg.env.unwrap_or_default(),
        }
    }

    /// A short human-readable summary of the invocation, e.g. `codex exec {prompt}`.
    pub fn invocation_hint(&self) -> String {
        let mut parts = vec![self.command.clone()];
        parts.extend(self.args.iter().cloned());
        if self.prompt_via == PromptVia::Stdin
            && !self.args.iter().any(|a| a.contains("{prompt}"))
        {
            parts.push("<stdin: prompt>".to_string());
        } else if !self.args.iter().any(|a| a.contains("{prompt}")) {
            parts.push("{prompt}".to_string());
        }
        parts.join(" ")
    }
}

/// Sanitize an agent name into a valid MCP tool name, e.g. `ask_codex`.
pub fn default_tool_name(agent: &str) -> String {
    let sanitized: String = agent
        .chars()
        .map(|c| if c.is_ascii_alphanumeric() { c } else { '_' })
        .collect();
    format!("ask_{sanitized}")
}

/// The built-in adapters shipped with agent-mcp.
pub fn builtins() -> HashMap<String, AdapterConfig> {
    fn a(command: &str, args: &[&str]) -> AdapterConfig {
        AdapterConfig {
            command: Some(command.to_string()),
            args: Some(args.iter().map(|s| s.to_string()).collect()),
            ..Default::default()
        }
    }
    let mut m = HashMap::new();
    // OpenAI Codex CLI: non-interactive `codex exec "<prompt>"`.
    m.insert("codex".to_string(), a("codex", &["exec", "{prompt}"]));
    // Claude Code print mode: `claude -p "<prompt>"`.
    m.insert("claude".to_string(), a("claude", &["-p", "{prompt}"]));
    // Gemini CLI non-interactive: `gemini -p "<prompt>"`.
    m.insert("gemini".to_string(), a("gemini", &["-p", "{prompt}"]));
    // xAI Grok CLI single-turn headless: `grok -p "<prompt>"`.
    m.insert("grok".to_string(), a("grok", &["-p", "{prompt}"]));
    m
}
