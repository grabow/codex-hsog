# Codex CLI (Rust Implementation)

This repository contains the Rust implementation that backs the `codex-hsog` student distribution.

## HSOG fork note

- This fork carries an HSOG-specific patch so the Rust CLI can continue to process gateway traffic that only exposes Completions or Chat Completions semantics where upstream Codex expects Responses.
- Student releases are distributed as native launcher bundles, not via the upstream `npm` package.
- The student-facing binary name is `codex-hsog` to avoid colliding with an existing upstream `codex` install.

## Student launcher usage

Students should download the release bundle for their platform from:

- <https://github.com/grabow/codex-hsog/releases/latest>

Bundle mapping:

- macOS Apple Silicon: `codex-hsog-macos-aarch64.tar.gz`
- macOS Intel: `codex-hsog-macos-x86_64.tar.gz`
- Linux x86_64: `codex-hsog-linux-x86_64.tar.gz`
- Linux ARM64: `codex-hsog-linux-aarch64.tar.gz`
- Windows x86_64: `codex-hsog-windows-x86_64.zip`

Windows ARM64 is currently not provided.

The recommended way for students to start the patched CLI is via the launcher included in each release bundle:

```shell
./start-codex-hsog.sh
```

or on Windows PowerShell:

```powershell
.\start-codex-hsog.ps1
```

The launcher prompts for `HSOG_API_KEY` if needed and injects the HSOG provider settings on the command line, so students do not need to edit `~/.codex/config.toml` for initial setup.

## Documentation quickstart

- First run with Codex? Start with [`docs/getting-started.md`](../docs/getting-started.md) (links to the walkthrough for prompts, keyboard shortcuts, and session management).
- Want deeper control? See [`docs/config.md`](../docs/config.md) and [`docs/install.md`](../docs/install.md).

## What's new in the Rust CLI

The Rust implementation is now the maintained Codex CLI and serves as the default experience. It includes a number of features that the legacy TypeScript CLI never supported.

### Config

Codex supports a rich set of configuration options. Note that the Rust CLI uses `config.toml` instead of `config.json`. See [`docs/config.md`](../docs/config.md) for details.

#### Provider fallback behavior (chat-only providers)

Some providers expose only Chat Completions while Codex internally targets the Responses API shape. In those setups:

- keep `wire_api = "responses"` and enable `fallback_chat = true` on the provider so Codex can translate Responses requests to Chat Completions on `404`.
- when Chat streaming contains real `tool_calls`, Codex treats the turn as a tool-call turn and suppresses mixed pseudo-tool text payloads.
- this also applies if a provider sends `finish_reason = "stop"` while still streaming tool-call deltas.
- in chat fallback request translation, only `type = "function"` tools are forwarded; non-function tools (for example `type = "custom"`) are ignored.

### Model Context Protocol Support

#### MCP client

Codex CLI functions as an MCP client that allows the Codex CLI and IDE extension to connect to MCP servers on startup. See the [`configuration documentation`](../docs/config.md#connecting-to-mcp-servers) for details.

#### MCP server (experimental)

Codex can be launched as an MCP _server_ by running `codex mcp-server`. This allows _other_ MCP clients to use Codex as a tool for another agent.

Use the [`@modelcontextprotocol/inspector`](https://github.com/modelcontextprotocol/inspector) to try it out:

```shell
npx @modelcontextprotocol/inspector codex mcp-server
```

Use `codex mcp` to add/list/get/remove MCP server launchers defined in `config.toml`, and `codex mcp-server` to run the MCP server directly.

### Notifications

You can enable notifications by configuring a script that is run whenever the agent finishes a turn. The [notify documentation](../docs/config.md#notify) includes a detailed example that explains how to get desktop notifications via [terminal-notifier](https://github.com/julienXX/terminal-notifier) on macOS. When Codex detects that it is running under WSL 2 inside Windows Terminal (`WT_SESSION` is set), the TUI automatically falls back to native Windows toast notifications so approval prompts and completed turns surface even though Windows Terminal does not implement OSC 9.

### `codex exec` to run Codex programmatically/non-interactively

To run Codex non-interactively, run `codex exec PROMPT` (you can also pass the prompt via `stdin`) and Codex will work on your task until it decides that it is done and exits. Output is printed to the terminal directly. You can set the `RUST_LOG` environment variable to see more about what's going on.
Use `codex exec --ephemeral ...` to run without persisting session rollout files to disk.

### Experimenting with the Codex Sandbox

To test to see what happens when a command is run under the sandbox provided by Codex, we provide the following subcommands in Codex CLI:

```
# macOS
codex sandbox macos [--full-auto] [--log-denials] [COMMAND]...

# Linux
codex sandbox linux [--full-auto] [COMMAND]...

# Windows
codex sandbox windows [--full-auto] [COMMAND]...

# Legacy aliases
codex debug seatbelt [--full-auto] [--log-denials] [COMMAND]...
codex debug landlock [--full-auto] [COMMAND]...
```

### Selecting a sandbox policy via `--sandbox`

The Rust CLI exposes a dedicated `--sandbox` (`-s`) flag that lets you pick the sandbox policy **without** having to reach for the generic `-c/--config` option:

```shell
# Run Codex with the default, read-only sandbox
codex --sandbox read-only

# Allow the agent to write within the current workspace while still blocking network access
codex --sandbox workspace-write

# Danger! Disable sandboxing entirely (only do this if you are already running in a container or other isolated env)
codex --sandbox danger-full-access
```

The same setting can be persisted in `~/.codex/config.toml` via the top-level `sandbox_mode = "MODE"` key, e.g. `sandbox_mode = "workspace-write"`.

## Code Organization

This folder is the root of a Cargo workspace. It contains quite a bit of experimental code, but here are the key crates:

- [`core/`](./core) contains the business logic for Codex. Ultimately, we hope this to be a library crate that is generally useful for building other Rust/native applications that use Codex.
- [`exec/`](./exec) "headless" CLI for use in automation.
- [`tui/`](./tui) CLI that launches a fullscreen TUI built with [Ratatui](https://ratatui.rs/).
- [`cli/`](./cli) CLI multitool that provides the aforementioned CLIs via subcommands.

If you want to contribute or inspect behavior in detail, start by reading the module-level `README.md` files under each crate and run the project workspace from the top-level `codex-rs` directory so shared config, features, and build scripts stay aligned.
