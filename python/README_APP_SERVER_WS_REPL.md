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
python/.venv/bin/python python/app_server_ws_repl.py \
  --url ws://<SERVER_IP>:4222 \
  --approval-policy never
```

Optional:

- `--thread-id <id>` resume existing thread on startup
- `--no-auto-approve` disable auto approval replies
- `--model <name>` set model override
- `--model-provider <name>` set provider override
- `--cwd <path>` set working directory override

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
- WebSocket app-server transport is currently marked experimental in `codex-rs/app-server/README.md`.
