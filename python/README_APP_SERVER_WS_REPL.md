# App-Server WS REPL (Python)

Lightweight interactive client for `codex app-server` over WebSocket (JSON-RPC).

## Use Case

- Run app-server on machine A.
- Connect from machine B.
- Type prompts and commands interactively.

## Start server (machine A)

```bash
cd /path/to/codex/codex-rs
./target/debug/codex app-server --listen ws://0.0.0.0:4222
```

Or via test client helper:

```bash
cargo run -p codex-app-server-test-client -- \
  --codex-bin ./target/debug/codex \
  serve --listen ws://0.0.0.0:4222 --kill
```

## Run REPL client (machine B)

```bash
cd /path/to/codex
uv run python/app_server_ws_repl.py \
  --url ws://<SERVER_IP>:4222 \
  --approval-policy never
```

Optional:

- `--thread-id <id>` resume existing thread on startup
- `--no-auto-approve` disable auto approval replies
- `--model <name>` set model override
- `--model-provider <name>` set provider override
- `--cwd <path>` set working directory override
- `--input-ui prompt-toolkit|auto|raw-tty` choose input UI (default: `prompt-toolkit`)
- `--gateway-activity-indicator / --no-gateway-activity-indicator` show `*+` send/receive activity lines (default: enabled)
- `--no-local-tool-routing` disable client-side routing for `exec_command`/`write_stdin`/`apply_patch`
- `--local-tool-shell-mode subprocess|persistent` local tool routing mode
  - `subprocess` (default): spawn a new shell per command
  - `persistent`: keep one interactive shell and reuse it for subsequent commands (per client/thread flow)
- `--local-tool-shell-init "<cmd>"` one-time init command for persistent shell
  - default: `auto` (loads common rc files based on detected shell)
  - example override: `source ~/.cshrc`
- `--token <value>` send bearer token when connecting to a gateway
- `--providers-json '<json-array>'` send `initialize.params.xGateway.providers`
- `--providers-json @/path/providers.json` read providers JSON from file
- `--provider-id <id>` send `thread/start.params.xGateway.providerId`
- `--provider-preflight / --no-provider-preflight` check selected gateway provider reachability before `thread/start` (default: enabled)
- `--provider-preflight-timeout-sec <seconds>` timeout for preflight `GET <baseUrl>/models` (default: 8.0)
- `--thread-config-json '<json-object>'` merge custom config overrides into `thread/start.params.config` and `thread/resume.params.config`
  - also supports `@/path/to/config.json`
- `--mcp-servers-json '<json-object>'` inject MCP server configs as `mcp_servers.<name>` config keys
  - also supports `@/path/to/mcp_servers.json`
  - example:
    - `{"moodle":{"url":"http://127.0.0.1:8765/mcp","required":false}}`

Example (Moodle MCP running on client machine B):

```bash
uv run --project /Users/wiggel/IntelliJIDEA/codex_clone/codex/python \
  /Users/wiggel/IntelliJIDEA/codex_clone/codex/python/app_server_ws_repl.py \
  --url ws://127.0.0.1:4321 \
  --token test-token01 \
  --providers-json @/Users/wiggel/Python/MoodleMan/providers.local.json \
  --provider-id HS_OG \
  --approval-policy never \
  --local-tool-routing \
  --mcp-servers-json @/Users/wiggel/Python/MoodleMan/mcp_servers.local.json
```

## Interactive commands

- `:help`
- `:new`
- `:resume <thread-id>`
- `:use <thread-id>`
- `:threads [limit]`
- `:interrupt [turn-id]`
- `:exec <shell command>`
- `:quit`

All non-`:` lines are sent as `turn/start` user text.

## Notes

- `item/agentMessage/delta` and `item/commandExecution/outputDelta` are streamed live.
- Approval requests are auto-answered by default; control with `--auto-approve/--no-auto-approve`.
- Local tool routing is enabled by default: new threads are started with dynamic tools so `exec_command`, `write_stdin`, and `apply_patch` run on machine B (the Python client host).
- Prompt input uses `prompt-toolkit` by default, which improves multi-line copy/paste and key handling across macOS/Linux/Windows terminals.
- For environment-sensitive workflows (venv/nvm/direnv), prefer `--local-tool-shell-mode persistent` so successive commands share one shell state.
- If auto init is not enough for your environment, set `--local-tool-shell-init` explicitly.
- For resumed older threads, local routing is guaranteed only if the thread already contains dynamic tools from a previous start with this client.
- If the gateway detects shared token usage, the client prints a `[gateway-warning] ...` line after connect.
- For remote MCP servers (running on the client/B machine), pass them via `--mcp-servers-json` so A can load tools for the started thread.
- WebSocket app-server transport is currently marked experimental in `codex-rs/app-server/README.md`.
