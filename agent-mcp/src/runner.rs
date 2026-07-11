//! Spawning the wrapped agent CLI and capturing its output with a timeout.

use std::io::{Read, Write};
use std::path::Path;
use std::process::{Command, Stdio};
use std::thread;
use std::time::Duration;

use anyhow::{Context, Result};
use wait_timeout::ChildExt;

use crate::adapter::{Adapter, PromptVia};

/// The result of running the wrapped agent once.
pub struct Outcome {
    pub stdout: String,
    pub stderr: String,
    pub success: bool,
    pub timed_out: bool,
    pub code: Option<i32>,
}

/// Run `adapter` with `prompt` in `cwd`. Never returns an error for a non-zero
/// exit or timeout — those are reported in [`Outcome`]. Returns `Err` only when
/// the process could not be spawned at all.
pub fn run(adapter: &Adapter, prompt: &str, cwd: &Path) -> Result<Outcome> {
    // Build the argument vector, substituting {prompt} where present.
    let mut args: Vec<String> = Vec::with_capacity(adapter.args.len() + 1);
    let mut used_placeholder = false;
    for a in &adapter.args {
        if a.contains("{prompt}") {
            used_placeholder = true;
            args.push(a.replace("{prompt}", prompt));
        } else {
            args.push(a.clone());
        }
    }
    let via_stdin = adapter.prompt_via == PromptVia::Stdin;
    // If there's no placeholder and we're passing via arg, append the prompt.
    if !used_placeholder && !via_stdin {
        args.push(prompt.to_string());
    }

    let mut cmd = Command::new(&adapter.command);
    cmd.args(&args)
        .current_dir(cwd)
        .stdin(if via_stdin {
            Stdio::piped()
        } else {
            Stdio::null()
        })
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    for (k, v) in &adapter.env {
        cmd.env(k, v);
    }

    let mut child = cmd
        .spawn()
        .with_context(|| format!("failed to spawn `{}`", adapter.command))?;

    // Feed the prompt via stdin if requested, then close it to signal EOF.
    if via_stdin {
        if let Some(mut stdin) = child.stdin.take() {
            let _ = stdin.write_all(prompt.as_bytes());
            // `stdin` dropped here -> EOF.
        }
    }

    // Drain stdout/stderr on separate threads so a full pipe buffer can't
    // deadlock the child while we wait on it.
    let mut out_pipe = child.stdout.take().expect("stdout piped");
    let mut err_pipe = child.stderr.take().expect("stderr piped");
    let out_handle = thread::spawn(move || drain(&mut out_pipe));
    let err_handle = thread::spawn(move || drain(&mut err_pipe));

    let status = child
        .wait_timeout(Duration::from_secs(adapter.timeout_secs))
        .context("waiting on child process")?;

    let (timed_out, code, success) = match status {
        Some(s) => (false, s.code(), s.success()),
        None => {
            let _ = child.kill();
            let _ = child.wait();
            (true, None, false)
        }
    };

    let stdout = out_handle.join().unwrap_or_default();
    let stderr = err_handle.join().unwrap_or_default();

    Ok(Outcome {
        stdout,
        stderr,
        success,
        timed_out,
        code,
    })
}

fn drain<R: Read>(r: &mut R) -> String {
    let mut buf = Vec::new();
    let _ = r.read_to_end(&mut buf);
    String::from_utf8_lossy(&buf).into_owned()
}
