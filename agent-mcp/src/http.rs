//! Streamable-HTTP transport for the MCP server.
//!
//! A long-running daemon: `agent-mcp <agent> --http` binds a local port and
//! serves the same JSON-RPC handlers as the stdio transport, so several clients
//! can connect to one instance. It implements the request/response half of the
//! MCP "Streamable HTTP" transport:
//!
//! - `POST /`: the client sends a JSON-RPC message; a request gets its JSON-RPC
//!   response back as `application/json`, a notification gets `202 Accepted`.
//! - `GET /`: `405` — this server sends no unsolicited (server-initiated)
//!   messages, which the spec explicitly permits.
//! - `DELETE /`: `202` — sessions are stateless, so there's nothing to tear down.

use std::sync::Arc;

use anyhow::{anyhow, Result};
use serde_json::Value;
use tiny_http::{Header, Method, Request, Response, Server as HttpServer};

use crate::mcp::Server;

type Resp = Response<std::io::Cursor<Vec<u8>>>;

/// Bind `addr` and serve until the process is killed.
pub fn serve(server: Arc<Server>, addr: &str) -> Result<()> {
    let http = HttpServer::http(addr).map_err(|e| anyhow!("failed to bind {addr}: {e}"))?;
    let http = Arc::new(http);

    // A small worker pool so slow agent calls don't serialize every client.
    let workers = std::thread::available_parallelism()
        .map(|n| n.get())
        .unwrap_or(4)
        .clamp(2, 8);

    let mut handles = Vec::with_capacity(workers);
    for _ in 0..workers {
        let http = Arc::clone(&http);
        let server = Arc::clone(&server);
        handles.push(std::thread::spawn(move || {
            while let Ok(mut request) = http.recv() {
                let response = build_response(&mut request, &server);
                let _ = request.respond(response);
            }
        }));
    }
    for h in handles {
        let _ = h.join();
    }
    Ok(())
}

fn build_response(request: &mut Request, server: &Server) -> Resp {
    match request.method() {
        Method::Post => handle_post(request, server),
        // No server-initiated stream; the spec allows returning 405 for GET.
        Method::Get => text(405, "method not allowed: this endpoint only accepts POST"),
        // Stateless sessions: nothing to delete.
        Method::Delete => Response::from_string("").with_status_code(202),
        _ => text(405, "method not allowed"),
    }
}

fn handle_post(request: &mut Request, server: &Server) -> Resp {
    let mut body = String::new();
    if request.as_reader().read_to_string(&mut body).is_err() {
        return json(200, &error(Value::Null, -32700, "could not read request body"));
    }

    let msg: Value = match serde_json::from_str(&body) {
        Ok(v) => v,
        Err(e) => return json(200, &error(Value::Null, -32700, &e.to_string())),
    };

    // JSON-RPC batching was removed in MCP 2025-06-18, but tolerate an array in
    // case an older client sends one: dispatch each and return the responses.
    if let Value::Array(items) = &msg {
        let responses: Vec<Value> = items.iter().filter_map(|m| server.dispatch(m)).collect();
        return if responses.is_empty() {
            Response::from_string("").with_status_code(202)
        } else {
            json(200, &Value::Array(responses))
        };
    }

    match server.dispatch(&msg) {
        Some(response) => json(200, &response),
        None => Response::from_string("").with_status_code(202),
    }
}

fn json(status: u16, value: &Value) -> Resp {
    let body = serde_json::to_string(value).unwrap_or_else(|_| "{}".to_string());
    let header = Header::from_bytes(&b"Content-Type"[..], &b"application/json"[..])
        .expect("valid header");
    Response::from_string(body)
        .with_status_code(status)
        .with_header(header)
}

fn text(status: u16, msg: &str) -> Resp {
    Response::from_string(msg).with_status_code(status)
}

fn error(id: Value, code: i64, message: &str) -> Value {
    serde_json::json!({ "jsonrpc": "2.0", "id": id, "error": { "code": code, "message": message } })
}
