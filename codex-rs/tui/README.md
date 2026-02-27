# Codex TUI Notes

The legacy, custom TUI/exec WebSocket stream (`ws://127.0.0.1:8765`) has been removed.

## What changed

- `codex` (TUI) no longer starts an embedded WebSocket server.
- `codex exec` no longer starts an embedded WebSocket server.
- The old TUI-specific mirror path and related routing hooks were removed.

## Recommended remote integration path

Use the built-in app-server protocol instead:

1. Start server: `codex app-server --port 4222`
2. Connect clients via WebSocket JSON-RPC (v2 protocol).

For a lightweight terminal client, see:

- `python/app_server_ws_repl.py`

Additional protocol details:

- `codex-rs/app-server/README.md`
