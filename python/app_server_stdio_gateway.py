#!/usr/bin/env python3
"""WebSocket gateway for Codex app-server over stdio.

Architecture:
- External clients connect via WebSocket to this gateway.
- Gateway authenticates clients via bearer token.
- For each user, gateway starts one `codex app-server` process using stdio.
- Gateway multiplexes JSON-RPC requests/responses between many clients and one backend process.

This keeps Codex upstream-compatible: backend transport is stdio, not Codex WS.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import ssl
import time
from typing import Any
from urllib.parse import parse_qs, urlparse
import uuid

try:
    import websockets
except Exception:  # pragma: no cover
    websockets = None


INVALID_REQUEST = -32600
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

ERR_NOT_INITIALIZED = -32020
ERR_ALREADY_INITIALIZED = -32021
ERR_AUTH_FAILED = -32022
ERR_PROVIDER_NOT_FOUND = -32023
ERR_BAD_GATEWAY_PAYLOAD = -32024
ERR_BACKEND_NOT_READY = -32025

_LOG = logging.getLogger("app_server_stdio_gateway")
_REDACTED = "<redacted>"
_SENSITIVE_KEYS = {
    "apikey",
    "api_key",
    "experimental_bearer_token",
    "authorization",
    "token",
    "access_token",
    "refresh_token",
}
_INLINE_SECRET_PATTERNS = [
    re.compile(r"(?i)(authorization\s*:\s*bearer\s+)(\S+)"),
    re.compile(r"(?i)(api[_-]?key\s*[=:]\s*)(\S+)"),
    re.compile(r"(?i)(access[_-]?token\s*[=:]\s*)(\S+)"),
    re.compile(r"(?i)(refresh[_-]?token\s*[=:]\s*)(\S+)"),
]


def _json_dumps(obj: dict[str, Any]) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _jsonrpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "id": request_id,
        "error": {
            "code": code,
            "message": message,
        },
    }


def _parse_bearer_token(headers: Any, path: str) -> str | None:
    auth_header = headers.get("Authorization") if headers is not None else None
    if isinstance(auth_header, str) and auth_header.lower().startswith("bearer "):
        token = auth_header[7:].strip()
        if token:
            return token

    parsed = urlparse(path)
    query = parse_qs(parsed.query)
    token_values = query.get("token")
    if token_values:
        token = token_values[0].strip()
        if token:
            return token

    return None


def _redact_value(value: Any, parent_key: str | None = None) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, inner in value.items():
            key_norm = key.lower()
            if key_norm in _SENSITIVE_KEYS:
                redacted[key] = _REDACTED
            else:
                redacted[key] = _redact_value(inner, parent_key=key_norm)
        return redacted

    if isinstance(value, list):
        return [_redact_value(item, parent_key=parent_key) for item in value]

    if isinstance(value, str):
        if parent_key in _SENSITIVE_KEYS:
            return _REDACTED
        redacted = value
        for pattern in _INLINE_SECRET_PATTERNS:
            redacted = pattern.sub(rf"\\1{_REDACTED}", redacted)
        return redacted

    return value


def _safe_error_text(exc: Exception) -> str:
    return str(_redact_value(str(exc)))


def _resolve_tls_paths(
    cert_file: str | None,
    key_file: str | None,
) -> tuple[Path, Path] | None:
    cert = cert_file.strip() if isinstance(cert_file, str) else ""
    key = key_file.strip() if isinstance(key_file, str) else ""
    if not cert and not key:
        return None
    if not cert or not key:
        raise ValueError("both --tls-cert-file and --tls-key-file are required")
    return Path(cert), Path(key)


def _build_server_ssl_context(
    cert_file: str | None,
    key_file: str | None,
) -> ssl.SSLContext | None:
    resolved = _resolve_tls_paths(cert_file, key_file)
    if resolved is None:
        return None

    cert_path, key_path = resolved
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
    return context


def _normalize_provider_entry(raw: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    provider_id = str(raw.get("providerId", "")).strip()
    if not provider_id:
        raise ValueError("providerId missing")

    base_url = str(raw.get("baseUrl", "")).strip()
    api_key = str(raw.get("apiKey", "")).strip()
    wire_api = str(raw.get("wireApi", "")).strip()

    if not base_url:
        raise ValueError(f"provider `{provider_id}` missing baseUrl")
    if not api_key:
        raise ValueError(f"provider `{provider_id}` missing apiKey")
    if wire_api not in {"responses", "chat_completions"}:
        raise ValueError(f"provider `{provider_id}` has invalid wireApi")

    normalized: dict[str, Any] = {
        "providerId": provider_id,
        "baseUrl": base_url,
        "apiKey": api_key,
        "wireApi": wire_api,
    }

    if "model" in raw:
        normalized["model"] = raw["model"]
    if "modelProvider" in raw:
        normalized["modelProvider"] = raw["modelProvider"]
    if "name" in raw:
        normalized["name"] = raw["name"]
    if "fallbackChat" in raw:
        normalized["fallbackChat"] = bool(raw["fallbackChat"])
    if "fallbackChatPath" in raw:
        normalized["fallbackChatPath"] = raw["fallbackChatPath"]

    return provider_id, normalized


def _extract_providers_from_initialize(params: dict[str, Any]) -> dict[str, dict[str, Any]]:
    x_gateway = params.get("xGateway")
    if x_gateway is None:
        return {}
    if not isinstance(x_gateway, dict):
        raise ValueError("xGateway must be an object")

    providers_raw = x_gateway.get("providers", [])
    if not isinstance(providers_raw, list):
        raise ValueError("xGateway.providers must be a list")

    providers: dict[str, dict[str, Any]] = {}
    for raw in providers_raw:
        if not isinstance(raw, dict):
            raise ValueError("xGateway.providers entries must be objects")
        provider_id, normalized = _normalize_provider_entry(raw)
        if provider_id in providers:
            raise ValueError(f"duplicate providerId `{provider_id}`")
        providers[provider_id] = normalized

    return providers


def _apply_provider_to_thread_start(
    params: dict[str, Any],
    providers: dict[str, dict[str, Any]],
) -> None:
    x_gateway = params.pop("xGateway", None)
    provider_id: str | None = None
    if isinstance(x_gateway, dict):
        raw_provider_id = x_gateway.get("providerId")
        if raw_provider_id is not None:
            provider_id = str(raw_provider_id)

    if provider_id is None:
        return

    provider = providers.get(provider_id)
    if provider is None:
        raise KeyError(provider_id)

    model_provider = str(provider.get("modelProvider") or provider_id)
    params["modelProvider"] = model_provider
    if provider.get("model") is not None:
        params["model"] = provider["model"]

    config = params.get("config")
    if not isinstance(config, dict):
        config = {}

    root = f"model_providers.{model_provider}"
    config[f"{root}.name"] = provider.get("name", model_provider)
    config[f"{root}.base_url"] = provider["baseUrl"]
    config[f"{root}.experimental_bearer_token"] = provider["apiKey"]
    config[f"{root}.wire_api"] = provider["wireApi"]

    if "fallbackChat" in provider:
        config[f"{root}.fallback_chat"] = bool(provider["fallbackChat"])
    if "fallbackChatPath" in provider and provider["fallbackChatPath"] is not None:
        config[f"{root}.fallback_chat_path"] = provider["fallbackChatPath"]

    params["config"] = config


@dataclass
class UserConfig:
    user_id: str
    token: str
    codex_home: Path
    workspace_root: Path


@dataclass(eq=False)
class ClientSession:
    websocket: Any
    user: UserConfig
    initialize_done: bool = False
    initialized_notified: bool = False
    providers: dict[str, dict[str, Any]] = field(default_factory=dict)


class UserWorker:
    def __init__(self, user: UserConfig, codex_bin: Path, idle_timeout_seconds: int):
        self.user = user
        self.codex_bin = codex_bin
        self.idle_timeout_seconds = idle_timeout_seconds

        self.process: asyncio.subprocess.Process | None = None
        self.stdout_task: asyncio.Task[None] | None = None
        self.stderr_task: asyncio.Task[None] | None = None

        self.clients: set[ClientSession] = set()
        self.pending_requests: dict[str, tuple[ClientSession, Any]] = {}
        self.pending_lock = asyncio.Lock()
        self.write_lock = asyncio.Lock()
        self.start_lock = asyncio.Lock()

        self.backend_initialized = False
        self.backend_user_agent = "codex-gateway/stdio"
        self.last_activity = time.monotonic()
        self._request_counter = 1

    def touch(self) -> None:
        self.last_activity = time.monotonic()

    async def add_client(self, client: ClientSession) -> None:
        async with self.start_lock:
            await self._ensure_started_locked()
            self.clients.add(client)
            self.touch()

    async def remove_client(self, client: ClientSession) -> None:
        self.clients.discard(client)
        self.touch()

    async def maybe_stop_if_idle(self) -> None:
        if self.process is None:
            return
        if self.clients:
            return
        idle = time.monotonic() - self.last_activity
        if idle < self.idle_timeout_seconds:
            return
        await self.stop("idle-timeout")

    async def stop(self, reason: str) -> None:
        _LOG.info("stopping backend for user=%s reason=%s", self.user.user_id, reason)
        process = self.process
        self.process = None
        self.backend_initialized = False

        if process is None:
            return

        if self.stdout_task is not None:
            self.stdout_task.cancel()
            self.stdout_task = None
        if self.stderr_task is not None:
            self.stderr_task.cancel()
            self.stderr_task = None

        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=2)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()

        async with self.pending_lock:
            dropped = list(self.pending_requests.values())
            self.pending_requests.clear()

        for client, original_id in dropped:
            await self._send_client(
                client,
                _jsonrpc_error(
                    original_id,
                    ERR_BACKEND_NOT_READY,
                    "backend terminated before response",
                ),
            )

    async def _ensure_started_locked(self) -> None:
        if self.process is not None and self.backend_initialized:
            return

        self.user.codex_home.mkdir(parents=True, exist_ok=True)
        self.user.workspace_root.mkdir(parents=True, exist_ok=True)

        env = os.environ.copy()
        env["CODEX_HOME"] = str(self.user.codex_home)
        _LOG.info(
            "starting backend for user=%s codex_home=%s",
            self.user.user_id,
            self.user.codex_home,
        )

        self.process = await asyncio.create_subprocess_exec(
            str(self.codex_bin),
            "app-server",
            "--listen",
            "stdio://",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

        self.stdout_task = asyncio.create_task(self._stdout_loop())
        self.stderr_task = asyncio.create_task(self._stderr_loop())

        init_id = f"gw-init-{uuid.uuid4()}"
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()

        async with self.pending_lock:
            self.pending_requests[init_id] = (None, future)

        await self._send_backend_message(
            {
                "id": init_id,
                "method": "initialize",
                "params": {
                    "clientInfo": {
                        "name": "codex_stdio_gateway",
                        "version": "0.1.0",
                    }
                },
            }
        )

        init_response = await asyncio.wait_for(future, timeout=10)
        result = init_response.get("result")
        if isinstance(result, dict):
            user_agent = result.get("userAgent")
            if isinstance(user_agent, str) and user_agent.strip():
                self.backend_user_agent = user_agent

        await self._send_backend_message({"method": "initialized", "params": {}})
        self.backend_initialized = True
        self.touch()
        _LOG.info("backend initialized for user=%s", self.user.user_id)

    async def _stderr_loop(self) -> None:
        if self.process is None or self.process.stderr is None:
            return
        try:
            while True:
                line = await self.process.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if text:
                    _LOG.warning(
                        "backend stderr user=%s: %s",
                        self.user.user_id,
                        _redact_value(text),
                    )
        except asyncio.CancelledError:
            return

    async def _stdout_loop(self) -> None:
        if self.process is None or self.process.stdout is None:
            return
        try:
            while True:
                line = await self.process.stdout.readline()
                if not line:
                    break
                self.touch()
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                try:
                    message = json.loads(text)
                except json.JSONDecodeError:
                    _LOG.warning(
                        "backend emitted non-json line user=%s",
                        self.user.user_id,
                    )
                    continue
                await self._handle_backend_message(message)
        except asyncio.CancelledError:
            return

    async def _handle_backend_message(self, message: dict[str, Any]) -> None:
        if "id" in message and ("result" in message or "error" in message):
            backend_id = str(message["id"])
            async with self.pending_lock:
                entry = self.pending_requests.pop(backend_id, None)

            if entry is None:
                return

            client_or_none, original = entry
            if client_or_none is None:
                if isinstance(original, asyncio.Future) and not original.done():
                    original.set_result(message)
                return

            outbound = dict(message)
            outbound["id"] = original
            await self._send_client(client_or_none, outbound)
            return

        # Notifications: broadcast to initialized clients.
        for client in list(self.clients):
            if not client.initialized_notified:
                continue
            await self._send_client(client, message)

    async def _send_backend_message(self, message: dict[str, Any]) -> None:
        if self.process is None or self.process.stdin is None:
            raise RuntimeError("backend process not running")
        payload = (_json_dumps(message) + "\n").encode("utf-8")
        async with self.write_lock:
            self.process.stdin.write(payload)
            await self.process.stdin.drain()

    async def _send_client(self, client: ClientSession, message: dict[str, Any]) -> None:
        try:
            await client.websocket.send(_json_dumps(message))
        except Exception:
            await self.remove_client(client)

    def _next_backend_id(self) -> str:
        value = self._request_counter
        self._request_counter += 1
        return f"gw-{value}"

    def _new_workspace(self) -> Path:
        name = f"session-{uuid.uuid4()}"
        path = self.user.workspace_root / name
        path.mkdir(parents=True, exist_ok=False)
        return path

    async def forward_request(self, client: ClientSession, request: dict[str, Any]) -> None:
        if not self.backend_initialized:
            await self._send_client(
                client,
                _jsonrpc_error(request.get("id"), ERR_BACKEND_NOT_READY, "backend not ready"),
            )
            return

        method = request.get("method")
        request_id = request.get("id")

        if method == "initialize":
            if client.initialize_done:
                await self._send_client(
                    client,
                    _jsonrpc_error(request_id, ERR_ALREADY_INITIALIZED, "Already initialized"),
                )
                return

            params = request.get("params")
            if not isinstance(params, dict):
                params = {}

            try:
                client.providers = _extract_providers_from_initialize(params)
            except ValueError as exc:
                _LOG.warning(
                    "initialize rejected user=%s error=%s",
                    client.user.user_id,
                    _safe_error_text(exc),
                )
                await self._send_client(
                    client,
                    _jsonrpc_error(request_id, ERR_BAD_GATEWAY_PAYLOAD, str(exc)),
                )
                return

            client.initialize_done = True
            await self._send_client(
                client,
                {
                    "id": request_id,
                    "result": {
                        "userAgent": self.backend_user_agent,
                    },
                },
            )
            return

        if method == "initialized":
            if not client.initialize_done:
                await self._send_client(
                    client,
                    _jsonrpc_error(None, ERR_NOT_INITIALIZED, "Not initialized"),
                )
                return
            client.initialized_notified = True
            return

        if not client.initialized_notified:
            await self._send_client(
                client,
                _jsonrpc_error(request_id, ERR_NOT_INITIALIZED, "Not initialized"),
            )
            return

        outbound = dict(request)
        params = outbound.get("params")
        if not isinstance(params, dict):
            params = {}
            outbound["params"] = params

        if method == "thread/start":
            workspace = self._new_workspace()
            params["cwd"] = str(workspace)
            _LOG.info(
                "new session workspace user=%s path=%s",
                client.user.user_id,
                workspace,
            )
            try:
                _apply_provider_to_thread_start(params, client.providers)
            except KeyError as exc:
                await self._send_client(
                    client,
                    _jsonrpc_error(
                        request_id,
                        ERR_PROVIDER_NOT_FOUND,
                        f"providerId not found: {exc.args[0]}",
                    ),
                )
                return

        if "id" not in outbound:
            await self._send_backend_message(outbound)
            return

        backend_id = self._next_backend_id()
        original_id = outbound["id"]
        outbound["id"] = backend_id

        async with self.pending_lock:
            self.pending_requests[backend_id] = (client, original_id)

        await self._send_backend_message(outbound)


class GatewayServer:
    def __init__(
        self,
        listen_host: str,
        listen_port: int,
        codex_bin: Path,
        users: list[UserConfig],
        workspace_root: Path,
        allow_self_register: bool,
        idle_timeout_seconds: int,
        ssl_context: ssl.SSLContext | None = None,
    ):
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.idle_timeout_seconds = idle_timeout_seconds
        self.codex_bin = codex_bin
        self.ssl_context = ssl_context
        self.workspace_root = workspace_root
        self.allow_self_register = allow_self_register

        self.users_by_token = {user.token: user for user in users}
        self.workers_by_user = {
            user.user_id: UserWorker(user, codex_bin, idle_timeout_seconds)
            for user in users
        }
        self._reaper_task: asyncio.Task[None] | None = None

    @staticmethod
    def _user_id_from_token(token: str) -> str:
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]
        return f"user_{digest}"

    def _resolve_user(self, token: str | None) -> UserConfig | None:
        if token is None:
            return None

        existing = self.users_by_token.get(token)
        if existing is not None:
            return existing
        if not self.allow_self_register:
            return None

        user_id = self._user_id_from_token(token)
        user = UserConfig(
            user_id=user_id,
            token=token,
            codex_home=self.workspace_root / user_id / "codex_home",
            workspace_root=self.workspace_root / user_id / "workspaces",
        )
        self.users_by_token[token] = user
        if user_id not in self.workers_by_user:
            self.workers_by_user[user_id] = UserWorker(user, self.codex_bin, self.idle_timeout_seconds)
        return user

    async def run(self) -> None:
        if websockets is None:
            raise RuntimeError("websockets dependency is missing; install `websockets`")

        self._reaper_task = asyncio.create_task(self._reaper_loop())
        scheme = "wss" if self.ssl_context is not None else "ws"
        _LOG.info(
            "gateway listening on %s://%s:%s",
            scheme,
            self.listen_host,
            self.listen_port,
        )
        async with websockets.serve(
            self._handle_client,
            self.listen_host,
            self.listen_port,
            ssl=self.ssl_context,
        ):
            await asyncio.Future()

    async def _reaper_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(5)
                for worker in self.workers_by_user.values():
                    await worker.maybe_stop_if_idle()
        except asyncio.CancelledError:
            return

    async def _handle_client(self, websocket: Any) -> None:
        request = getattr(websocket, "request", None)
        path = getattr(request, "path", "") if request is not None else ""
        headers = getattr(request, "headers", None)
        if headers is None:
            headers = getattr(websocket, "request_headers", None)

        token = _parse_bearer_token(headers, path)
        user = self._resolve_user(token)
        if user is None:
            _LOG.warning("connection rejected: unauthorized")
            await websocket.close(code=1008, reason="unauthorized")
            return

        worker = self.workers_by_user[user.user_id]
        client = ClientSession(websocket=websocket, user=user)
        try:
            await worker.add_client(client)
        except Exception as exc:
            _LOG.exception(
                "failed to start backend for user=%s error=%s",
                user.user_id,
                _safe_error_text(exc),
            )
            await websocket.send(
                _json_dumps(
                    _jsonrpc_error(
                        None,
                        ERR_BACKEND_NOT_READY,
                        "backend startup failed",
                    )
                )
            )
            await websocket.close(code=1011, reason="backend startup failed")
            return

        _LOG.info("connection accepted user=%s", user.user_id)

        try:
            async for raw_message in websocket:
                if not isinstance(raw_message, str):
                    continue
                worker.touch()
                try:
                    message = json.loads(raw_message)
                except json.JSONDecodeError:
                    await worker._send_client(
                        client,
                        _jsonrpc_error(None, INVALID_REQUEST, "invalid JSON"),
                    )
                    continue

                if not isinstance(message, dict):
                    await worker._send_client(
                        client,
                        _jsonrpc_error(None, INVALID_REQUEST, "JSON-RPC object expected"),
                    )
                    continue

                if "method" in message:
                    try:
                        await worker.forward_request(client, message)
                    except Exception as exc:
                        _LOG.exception(
                            "request handling failed user=%s error=%s",
                            user.user_id,
                            _safe_error_text(exc),
                        )
                        await worker._send_client(
                            client,
                            _jsonrpc_error(
                                message.get("id"),
                                INTERNAL_ERROR,
                                "internal gateway error",
                            ),
                        )
                    continue

                if "id" in message and ("result" in message or "error" in message):
                    # Client response messages are not expected in this gateway mode.
                    continue

                await worker._send_client(
                    client,
                    _jsonrpc_error(message.get("id"), INVALID_REQUEST, "invalid JSON-RPC message"),
                )
        finally:
            _LOG.info("connection closed user=%s", user.user_id)
            await worker.remove_client(client)


def load_user_configs(path: Path, workspace_root: Path) -> list[UserConfig]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    users_raw = raw.get("users") if isinstance(raw, dict) else None
    if not isinstance(users_raw, list) or not users_raw:
        raise ValueError("users config must contain non-empty `users` list")

    users: list[UserConfig] = []
    for entry in users_raw:
        if not isinstance(entry, dict):
            raise ValueError("users entries must be objects")

        user_id = str(entry.get("id", "")).strip()
        token = str(entry.get("token", "")).strip()
        if not user_id or not token:
            raise ValueError("each user needs non-empty `id` and `token`")

        codex_home_raw = entry.get("codexHome")
        if codex_home_raw:
            codex_home = Path(str(codex_home_raw))
        else:
            codex_home = workspace_root / user_id / "codex_home"

        workspace_raw = entry.get("workspaceRoot")
        if workspace_raw:
            user_workspace_root = Path(str(workspace_raw))
        else:
            user_workspace_root = workspace_root / user_id / "workspaces"

        users.append(
            UserConfig(
                user_id=user_id,
                token=token,
                codex_home=codex_home,
                workspace_root=user_workspace_root,
            )
        )

    return users


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="WS gateway for codex app-server (backend transport: stdio)",
    )
    parser.add_argument(
        "--listen-host",
        default="127.0.0.1",
        help="gateway listen host",
    )
    parser.add_argument(
        "--listen-port",
        type=int,
        default=4321,
        help="gateway listen port",
    )
    parser.add_argument(
        "--codex-bin",
        default="./target/debug/codex",
        help="path to codex binary",
    )
    parser.add_argument(
        "--users-config",
        default=None,
        help="path to users JSON config",
    )
    parser.add_argument(
        "--workspace-root",
        default="./.gateway-state",
        help="default base dir for per-user codex_home/workspaces",
    )
    parser.add_argument(
        "--idle-timeout-seconds",
        type=int,
        default=300,
        help="stop per-user backend process after this idle time",
    )
    parser.add_argument(
        "--tls-cert-file",
        default=None,
        help="path to TLS certificate (enables wss when used with --tls-key-file)",
    )
    parser.add_argument(
        "--tls-key-file",
        default=None,
        help="path to TLS private key (enables wss when used with --tls-cert-file)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="gateway log level",
    )
    parser.add_argument(
        "--no-self-register",
        action="store_false",
        dest="self_register",
        help="disable token-based self-registration and require users from --users-config",
    )
    parser.set_defaults(self_register=True)
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=getattr(logging, str(args.log_level).upper(), logging.INFO),
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
    else:
        _LOG.setLevel(getattr(logging, str(args.log_level).upper(), logging.INFO))

    workspace_root = Path(args.workspace_root)
    users: list[UserConfig] = []
    if args.users_config:
        users = load_user_configs(Path(args.users_config), workspace_root)
    if not args.self_register and not users:
        raise ValueError("strict auth requires --users-config with at least one user")

    ssl_context = _build_server_ssl_context(args.tls_cert_file, args.tls_key_file)
    server = GatewayServer(
        listen_host=args.listen_host,
        listen_port=args.listen_port,
        codex_bin=Path(args.codex_bin),
        users=users,
        workspace_root=workspace_root,
        allow_self_register=bool(args.self_register),
        idle_timeout_seconds=args.idle_timeout_seconds,
        ssl_context=ssl_context,
    )
    await server.run()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
