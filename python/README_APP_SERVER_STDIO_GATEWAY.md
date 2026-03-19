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

- Token-based authentication (`Bearer` header or `?token=...`)
- Self-registration by token (default, no `users.json` required)
- Per-user backend process with idle shutdown
- `thread/start` prefers client-provided absolute `cwd` when valid
  - falls back to a new gateway workspace when missing/invalid
- Optional provider mapping from `initialize.params.xGateway.providers`
  - provider selection via `thread/start.params.xGateway.providerId`

## Start

```bash
cd /path/to/codex
uv run python/app_server_stdio_gateway.py \
  --codex-bin ./codex-rs/target/debug/codex \
  --listen-host 0.0.0.0 \
  --listen-port 4321 \
  --log-level INFO
```

### Start with TLS (`wss`)

```bash
uv run python/app_server_stdio_gateway.py \
  --codex-bin ./codex-rs/target/debug/codex \
  --listen-host 0.0.0.0 \
  --listen-port 4321 \
  --tls-cert-file /path/to/fullchain.pem \
  --tls-key-file /path/to/privkey.pem
```

Both `--tls-cert-file` and `--tls-key-file` are required together.

## Optional `users.json` preload

If you want a preconfigured allow-list (or fixed IDs), pass `--users-config`.

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

Without `--users-config`, users are created automatically on first connect:

- Token is the identity input.
- Gateway derives a stable internal user ID from token hash.
- Same token always maps to the same backend state (`codex_home` + workspaces).

## Client auth

Use either:

- `Authorization: Bearer <token>` header (preferred)
- query parameter `?token=<token>`

Example token generation:

```bash
uuidgen
```

Strict mode (disable self-registration):

```bash
uv run python/app_server_stdio_gateway.py \
  --users-config ./users.json \
  --no-self-register
```

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
          "requestMaxRetries": 1,
          "streamMaxRetries": 0,
          "streamIdleTimeoutMs": 15000,
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

The gateway rewrites this into backend-compatible `modelProvider` + `config` overrides.
For `cwd`, gateway behavior is:

- if client sends an absolute, existing directory in `thread/start.params.cwd`, it is used as-is
- otherwise, gateway creates and uses a new per-session workspace directory

Optional fail-fast fields in provider entries:

- `requestMaxRetries` -> `model_providers.<id>.request_max_retries`
- `streamMaxRetries` -> `model_providers.<id>.stream_max_retries`
- `streamIdleTimeoutMs` -> `model_providers.<id>.stream_idle_timeout_ms`

## Notes

- Secrets are currently in-memory only per connected client.
- Backend transport remains stdio-only (`codex app-server --listen stdio://`).
- Logging applies best-effort redaction for common secret fields/tokens.
- Token management (rotation/revocation/audit) is still intentionally simple in this phase.
- If a token is reused across live WebSocket clients, the gateway logs a `!!!` warning and includes `gatewayWarning` in the `initialize` result for the client.
- For project-local skills, this means you can keep skills in `<project>/skills` and start the client in that project (or pass `--cwd`) so `skills/list` resolves the same context as local Codex.
