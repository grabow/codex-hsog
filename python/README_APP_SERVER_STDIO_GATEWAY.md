# App-Server STDIO Gateway (Python)

WebSocket gateway for `codex app-server` with backend transport fixed to **stdio**.

## Why

- Keep compatibility with upstream Codex app-server API.
- Avoid depending on app-server WebSocket transport.
- Run one backend process per authenticated user.

## Architecture

- Client -> Gateway: WebSocket JSON-RPC
- Gateway -> Codex backend: `codex app-server --listen stdio://`
- Per user: dedicated backend process (`CODEX_HOME` scoped per user)

## Features in this first version

- Bearer-token authentication per user
- Per-user backend process with idle shutdown
- `thread/start` rewrite to force a new workspace per session
- Optional provider mapping from `initialize.params.xGateway.providers`
  - provider selection via `thread/start.params.xGateway.providerId`

## Start

```bash
cd /path/to/codex
python/.venv/bin/python python/app_server_stdio_gateway.py \
  --users-config ./users.json \
  --codex-bin ./codex-rs/target/debug/codex \
  --listen-host 0.0.0.0 \
  --listen-port 4321
```

## users.json

```json
{
  "users": [
    {
      "id": "user-b",
      "token": "token-b"
    },
    {
      "id": "user-c",
      "token": "token-c",
      "codexHome": "/srv/codex/users/user-c/codex_home",
      "workspaceRoot": "/srv/codex/users/user-c/workspaces"
    }
  ]
}
```

If `codexHome`/`workspaceRoot` are omitted, defaults are created under `--workspace-root`.

## Client auth

Use either:

- `Authorization: Bearer <token>` header (preferred)
- query parameter `?token=<token>`

## Gateway extension fields

The gateway accepts extra fields under `xGateway` (not forwarded as-is to backend):

### Initialize

```json
{
  "method": "initialize",
  "id": 1,
  "params": {
    "clientInfo": {"name": "client", "version": "0.1.0"},
    "xGateway": {
      "providers": [
        {
          "providerId": "academic",
          "baseUrl": "https://llm-proxy.example/v1",
          "apiKey": "secret",
          "wireApi": "chat_completions",
          "fallbackChat": true,
          "fallbackChatPath": "/chat/completions",
          "model": "gpt-4.1"
        }
      ]
    }
  }
}
```

### thread/start

```json
{
  "method": "thread/start",
  "id": 2,
  "params": {
    "xGateway": {"providerId": "academic"},
    "input": [{"type": "text", "text": "hello"}]
  }
}
```

The gateway rewrites this into backend-compatible `modelProvider` + `config` overrides and forces `cwd` to a new workspace directory.

## Notes

- Secrets are currently in-memory only per connected client.
- This is an initial implementation focused on stdio transport and process isolation.
- For production hardening, add TLS termination, strict logging redaction policy, and token management.
