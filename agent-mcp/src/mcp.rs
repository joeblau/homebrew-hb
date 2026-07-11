//! A minimal MCP server over stdio (newline-delimited JSON-RPC 2.0).
//!
//! We hand-roll the small slice of the protocol we need — `initialize`,
//! `tools/list`, `tools/call`, and `ping` — so we can expose a single,
//! dynamically-named tool (`ask_<agent>`) with zero heavy dependencies.

use std::io::{BufRead, Write};
use std::path::PathBuf;

use serde_json::{json, Value};

use crate::adapter::Adapter;
use crate::runner;

/// The MCP protocol version we advertise. We echo the client's requested
/// version when it sends one, falling back to this.
const PROTOCOL_VERSION: &str = "2025-06-18";
const SERVER_NAME: &str = "agent-mcp";
const SERVER_VERSION: &str = env!("CARGO_PKG_VERSION");

pub struct Server {
    adapter: Adapter,
    cwd: PathBuf,
}

impl Server {
    pub fn new(adapter: Adapter, cwd: PathBuf) -> Self {
        Server { adapter, cwd }
    }

    /// Run the blocking stdio serve loop until stdin reaches EOF.
    pub fn serve(&self) -> anyhow::Result<()> {
        let stdin = std::io::stdin();
        let mut stdout = std::io::stdout();
        let mut line = String::new();

        loop {
            line.clear();
            let n = stdin.lock().read_line(&mut line)?;
            if n == 0 {
                break; // EOF
            }
            let trimmed = line.trim();
            if trimmed.is_empty() {
                continue;
            }

            let msg: Value = match serde_json::from_str(trimmed) {
                Ok(v) => v,
                Err(e) => {
                    // Parse error: reply per JSON-RPC with a null id.
                    self.write(&mut stdout, error_response(Value::Null, -32700, &e.to_string()))?;
                    continue;
                }
            };

            if let Some(response) = self.dispatch(&msg) {
                self.write(&mut stdout, response)?;
            }
        }
        Ok(())
    }

    /// Process one parsed JSON-RPC message. Returns `Some(response)` for a
    /// request (has both `method` and `id`), or `None` for a notification or a
    /// message we don't answer. This is the transport-agnostic entry point
    /// shared by the stdio and HTTP servers.
    pub fn dispatch(&self, msg: &Value) -> Option<Value> {
        // A response has no `method`; a notification has a `method` but no `id`.
        let method = msg.get("method").and_then(Value::as_str)?;
        match msg.get("id").cloned() {
            Some(id) => Some(self.handle_request(method, msg.get("params"), id)),
            None => {
                self.handle_notification(method);
                None
            }
        }
    }

    fn handle_notification(&self, method: &str) {
        match method {
            "notifications/initialized" | "notifications/cancelled" => {}
            other => eprintln!("agent-mcp: ignoring notification `{other}`"),
        }
    }

    fn handle_request(&self, method: &str, params: Option<&Value>, id: Value) -> Value {
        match method {
            "initialize" => self.on_initialize(params, id),
            "ping" => result_response(id, json!({})),
            "tools/list" => result_response(id, json!({ "tools": [self.tool_schema()] })),
            "tools/call" => self.on_tools_call(params, id),
            other => error_response(id, -32601, &format!("method not found: {other}")),
        }
    }

    fn on_initialize(&self, params: Option<&Value>, id: Value) -> Value {
        let version = params
            .and_then(|p| p.get("protocolVersion"))
            .and_then(Value::as_str)
            .unwrap_or(PROTOCOL_VERSION)
            .to_string();
        result_response(
            id,
            json!({
                "protocolVersion": version,
                "capabilities": { "tools": { "listChanged": false } },
                "serverInfo": { "name": SERVER_NAME, "version": SERVER_VERSION },
            }),
        )
    }

    fn tool_schema(&self) -> Value {
        json!({
            "name": self.adapter.tool_name,
            "description": self.adapter.description,
            "inputSchema": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "The task or question to send to the agent. \
                                        Be specific and self-contained.",
                    }
                },
                "required": ["prompt"],
                "additionalProperties": false,
            }
        })
    }

    fn on_tools_call(&self, params: Option<&Value>, id: Value) -> Value {
        let params = match params {
            Some(p) => p,
            None => return error_response(id, -32602, "missing params"),
        };
        let name = params.get("name").and_then(Value::as_str).unwrap_or("");
        if name != self.adapter.tool_name {
            return error_response(id, -32602, &format!("unknown tool: {name}"));
        }
        let prompt = params
            .get("arguments")
            .and_then(|a| a.get("prompt"))
            .and_then(Value::as_str);
        let prompt = match prompt {
            Some(p) if !p.trim().is_empty() => p,
            _ => return tool_error(id, "the `prompt` argument is required and must be non-empty"),
        };

        match runner::run(&self.adapter, prompt, &self.cwd) {
            Ok(outcome) => {
                let mut text = outcome.stdout;
                if outcome.timed_out {
                    text.push_str(&format!(
                        "\n\n[agent-mcp: `{}` timed out after {}s]",
                        self.adapter.command, self.adapter.timeout_secs
                    ));
                }
                if !outcome.success && !outcome.stderr.trim().is_empty() {
                    text.push_str("\n\n[stderr]\n");
                    text.push_str(outcome.stderr.trim_end());
                }
                if text.trim().is_empty() {
                    text = format!(
                        "[agent-mcp: `{}` produced no output (exit {:?})]",
                        self.adapter.command, outcome.code
                    );
                }
                tool_text(id, &text, !outcome.success)
            }
            Err(e) => tool_error(id, &format!("failed to run `{}`: {e:#}", self.adapter.command)),
        }
    }

    fn write<W: Write>(&self, w: &mut W, value: Value) -> std::io::Result<()> {
        let s = serde_json::to_string(&value)?;
        w.write_all(s.as_bytes())?;
        w.write_all(b"\n")?;
        w.flush()
    }
}

fn result_response(id: Value, result: Value) -> Value {
    json!({ "jsonrpc": "2.0", "id": id, "result": result })
}

fn error_response(id: Value, code: i64, message: &str) -> Value {
    json!({ "jsonrpc": "2.0", "id": id, "error": { "code": code, "message": message } })
}

/// A successful `tools/call` result carrying text content. `is_error` marks a
/// tool-level (not protocol-level) failure so the client can surface it.
fn tool_text(id: Value, text: &str, is_error: bool) -> Value {
    result_response(
        id,
        json!({
            "content": [{ "type": "text", "text": text }],
            "isError": is_error,
        }),
    )
}

fn tool_error(id: Value, text: &str) -> Value {
    tool_text(id, &format!("[agent-mcp error] {text}"), true)
}
