//! Loading and layering of config files.

use std::collections::HashMap;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result};
use serde::Deserialize;

use crate::adapter::AdapterConfig;

#[derive(Debug, Default, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FileConfig {
    #[serde(default)]
    pub agents: HashMap<String, AdapterConfig>,
}

impl FileConfig {
    fn load(path: &Path) -> Result<FileConfig> {
        let text = std::fs::read_to_string(path)
            .with_context(|| format!("reading config {}", path.display()))?;
        let cfg: FileConfig = toml::from_str(&text)
            .with_context(|| format!("parsing config {}", path.display()))?;
        Ok(cfg)
    }
}

/// The merged set of agent adapter configs, ordered by precedence.
#[derive(Debug, Default)]
pub struct Layered {
    agents: HashMap<String, AdapterConfig>,
}

impl Layered {
    /// Build the layered config: built-ins < user < project < explicit.
    ///
    /// `cwd` is where the project-local `.agent-mcp.toml` is looked up.
    pub fn build(cwd: &Path, explicit: Option<&Path>) -> Result<Layered> {
        let mut agents = crate::adapter::builtins();

        let mut overlay = |cfg: FileConfig| {
            for (name, ac) in cfg.agents {
                let base = agents.remove(&name).unwrap_or_default();
                agents.insert(name, base.merged(ac));
            }
        };

        if let Some(path) = user_config_path() {
            if path.exists() {
                overlay(FileConfig::load(&path)?);
            }
        }

        let project_path = cwd.join(".agent-mcp.toml");
        if project_path.exists() {
            overlay(FileConfig::load(&project_path)?);
        }

        if let Some(path) = explicit {
            overlay(FileConfig::load(path)?);
        }

        Ok(Layered { agents })
    }

    /// The merged config for a specific agent (empty if none — a generic agent).
    pub fn for_agent(&self, agent: &str) -> AdapterConfig {
        self.agents.get(agent).cloned().unwrap_or_default()
    }

    /// Whether this agent name is known (built-in or configured).
    pub fn is_known(&self, agent: &str) -> bool {
        self.agents.contains_key(agent)
    }

    /// Sorted list of known agent names.
    pub fn names(&self) -> Vec<String> {
        let mut v: Vec<String> = self.agents.keys().cloned().collect();
        v.sort();
        v
    }
}

/// `$XDG_CONFIG_HOME/agent-mcp/config.toml`, falling back to
/// `$HOME/.config/agent-mcp/config.toml`.
fn user_config_path() -> Option<PathBuf> {
    if let Ok(xdg) = std::env::var("XDG_CONFIG_HOME") {
        if !xdg.is_empty() {
            return Some(PathBuf::from(xdg).join("agent-mcp").join("config.toml"));
        }
    }
    let home = std::env::var("HOME").ok()?;
    Some(
        PathBuf::from(home)
            .join(".config")
            .join("agent-mcp")
            .join("config.toml"),
    )
}
