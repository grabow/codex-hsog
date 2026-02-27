#!/usr/bin/env python3
"""Interactive remote client for codex app-server over WebSocket (JSON-RPC v2).

This client is designed for the workflow:
- Start `codex app-server --listen ws://...` on machine A
- Connect from machine B
- Type prompts and commands interactively

It performs the required initialize/initialized handshake and supports:
- creating/resuming threads
- starting turns with text input
- interrupting active turns
- direct command execution via `command/exec`
- auto-responding to approval requests
"""

from __future__ import annotations

import argparse
import asyncio
import atexit
import contextlib
from dataclasses import dataclass
from dataclasses import field
import json
import os
from pathlib import Path
import platform
import re
import shlex
import sys
import termios
import tty
import uuid
from typing import Any
from urllib.parse import parse_qsl
from urllib.parse import urlencode
from urllib.parse import urlparse
from urllib.parse import urlunparse

try:
    import websockets
except Exception:  # pragma: no cover - optional for parser-only tests
    websockets = None


@dataclass
class ReplCommand:
    kind: str
    arg: str | None = None


@dataclass
class LocalExecSession:
    session_id: int
    process: asyncio.subprocess.Process
    output_queue: asyncio.Queue[str]
    started_at: float
    completion_event: asyncio.Event
    reader_tasks: list[asyncio.Task[None]]
    waiter_task: asyncio.Task[None]
    persistent_shell: bool = False
    shell_path: str | None = None
    shell_kind: str = "posix"
    login_shell: bool = True
    pending_buffer: str = ""
    active_marker: str | None = None
    active_started_at: float | None = None
    command_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def parse_repl_command(line: str) -> ReplCommand:
    """Parse `:...` commands used by the interactive prompt."""
    stripped = line.strip()
    if not stripped.startswith(":"):
        return ReplCommand(kind="message", arg=line)

    body = stripped[1:].strip()
    if not body:
        return ReplCommand(kind="help")

    parts = body.split(maxsplit=1)
    head = parts[0].lower()
    tail = parts[1] if len(parts) > 1 else None

    if head in {"q", "quit", "exit"}:
        return ReplCommand(kind="quit")
    if head == "help":
        return ReplCommand(kind="help")
    if head == "new":
        return ReplCommand(kind="new")
    if head == "resume":
        return ReplCommand(kind="resume", arg=tail)
    if head == "use":
        return ReplCommand(kind="use", arg=tail)
    if head == "threads":
        return ReplCommand(kind="threads", arg=tail)
    if head == "interrupt":
        return ReplCommand(kind="interrupt", arg=tail)
    if head == "exec":
        return ReplCommand(kind="exec", arg=tail)
    return ReplCommand(kind="unknown", arg=body)


def format_ws_endpoint(uri: str) -> str:
    parsed = urlparse(uri)
    host = parsed.hostname
    if host is None:
        return uri
    if parsed.port is not None:
        port = parsed.port
    elif parsed.scheme == "wss":
        port = 443
    else:
        port = 80
    return f"{host}:{port}"


def uri_with_token(uri: str, token: str) -> str:
    parsed = urlparse(uri)
    query_items = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query_items["token"] = token
    return urlunparse(parsed._replace(query=urlencode(query_items)))


def load_gateway_providers(raw: str | None) -> list[dict[str, Any]] | None:
    if raw is None:
        return None
    value = raw.strip()
    if not value:
        return None

    if value.startswith("@"):
        file_path = Path(value[1:])
        try:
            value = file_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise RuntimeError(f"could not read providers file `{file_path}`: {exc}") from exc

    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid --providers-json value: {exc}") from exc

    if not isinstance(decoded, list):
        raise RuntimeError("--providers-json must decode to a JSON list")

    providers: list[dict[str, Any]] = []
    for index, entry in enumerate(decoded):
        if not isinstance(entry, dict):
            raise RuntimeError(f"--providers-json entry at index {index} must be an object")
        providers.append(entry)
    return providers


def format_stream_text(text: str) -> str:
    """Improve readability of streamed markdown-like status markers."""
    if "**" not in text:
        return text
    return re.sub(r"(\*\*[^*\n][^*\n]*\*\*)(?!\r?\n)", r"\1\r\n", text)


def format_ephemeral_status_text(text: str) -> str:
    """Create a compact single-line status text for transient reasoning/plan updates."""
    bold_chunks = re.findall(r"\*\*([^*\n][^*\n]*?)\*\*", text)
    if bold_chunks:
        return bold_chunks[-1].strip()

    compact = " ".join(text.replace("\r", " ").replace("\n", " ").split())
    return compact.strip()


_HISTORY_PATH = Path.home() / ".codex_ws_repl_history"
_INPUT_HISTORY: list[str] = []
_HISTORY_CONFIGURED = False


def configure_readline() -> None:
    """Load/persist command history for the custom input editor."""
    global _HISTORY_CONFIGURED
    if _HISTORY_CONFIGURED:
        return

    try:
        if _HISTORY_PATH.exists():
            _INPUT_HISTORY.extend(
                line.rstrip("\n")
                for line in _HISTORY_PATH.read_text(encoding="utf-8").splitlines()
                if line.strip()
            )
    except Exception:
        pass

    def save_history() -> None:
        try:
            recent: list[str] = []
            for entry in _INPUT_HISTORY[-1000:]:
                if not recent or recent[-1] != entry:
                    recent.append(entry)
            _HISTORY_PATH.write_text("\n".join(recent) + ("\n" if recent else ""), encoding="utf-8")
        except Exception:
            pass

    atexit.register(save_history)
    _HISTORY_CONFIGURED = True


def _redraw_input_line(prompt: str, buffer: list[str], cursor: int) -> None:
    line = "".join(buffer)
    sys.stdout.write("\r\x1b[2K")
    sys.stdout.write(f"{prompt}{line}")
    back = len(line) - cursor
    if back > 0:
        sys.stdout.write(f"\x1b[{back}D")
    sys.stdout.flush()


def _emit_status_line(text: str) -> None:
    sys.stdout.write(f"\r\n{text}\r\n")
    sys.stdout.flush()


def _move_word_left(buffer: list[str], cursor: int) -> int:
    while cursor > 0 and buffer[cursor - 1].isspace():
        cursor -= 1
    while cursor > 0 and not buffer[cursor - 1].isspace():
        cursor -= 1
    return cursor


def _move_word_right(buffer: list[str], cursor: int) -> int:
    size = len(buffer)
    while cursor < size and buffer[cursor].isspace():
        cursor += 1
    while cursor < size and not buffer[cursor].isspace():
        cursor += 1
    return cursor


def _push_history(line: str) -> None:
    if not line.strip():
        return
    if _INPUT_HISTORY and _INPUT_HISTORY[-1] == line:
        return
    _INPUT_HISTORY.append(line)


def read_user_input(prompt: str) -> str:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        line = input(prompt)
        _push_history(line)
        return line

    buffer: list[str] = []
    cursor = 0
    history_index: int | None = None
    history_draft = ""

    sys.stdout.write(prompt)
    sys.stdout.flush()

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        while True:
            ch = sys.stdin.read(1)

            if ch in {"\r", "\n"}:
                sys.stdout.write("\r\n")
                sys.stdout.flush()
                line = "".join(buffer)
                _push_history(line)
                return line

            if ch == "\x03":
                raise KeyboardInterrupt

            if ch == "\x04":
                raise EOFError

            if ch == "\x18":
                buffer.clear()
                cursor = 0
                history_index = None
                history_draft = ""
                _redraw_input_line(prompt, buffer, cursor)
                continue

            if ch in {"\x7f", "\b"}:
                if cursor > 0:
                    del buffer[cursor - 1]
                    cursor -= 1
                    history_index = None
                    _redraw_input_line(prompt, buffer, cursor)
                continue

            if ch == "\x1b":
                prefix = sys.stdin.read(1)

                # Alt/meta shortcuts often arrive as ESC+b / ESC+f.
                if prefix in {"b", "B"}:
                    next_cursor = _move_word_left(buffer, cursor)
                    if next_cursor != cursor:
                        cursor = next_cursor
                        _redraw_input_line(prompt, buffer, cursor)
                    continue

                if prefix in {"f", "F"}:
                    next_cursor = _move_word_right(buffer, cursor)
                    if next_cursor != cursor:
                        cursor = next_cursor
                        _redraw_input_line(prompt, buffer, cursor)
                    continue

                if prefix == "\x1b":
                    prefix = sys.stdin.read(1)

                if prefix not in {"[", "O"}:
                    continue

                key = sys.stdin.read(1)
                modifier: str | None = None

                if key not in {"A", "B", "C", "D"} and key.isdigit():
                    seq = key
                    while True:
                        part = sys.stdin.read(1)
                        if part.isalpha() or part == "~":
                            key = part
                            break
                        seq += part
                    if ";" in seq:
                        modifier = seq.split(";")[-1]

                if key == "A":
                    if _INPUT_HISTORY:
                        if history_index is None:
                            history_draft = "".join(buffer)
                            history_index = len(_INPUT_HISTORY) - 1
                        elif history_index > 0:
                            history_index -= 1
                        buffer = list(_INPUT_HISTORY[history_index])
                        cursor = len(buffer)
                        _redraw_input_line(prompt, buffer, cursor)
                    continue

                if key == "B":
                    if history_index is not None:
                        if history_index < len(_INPUT_HISTORY) - 1:
                            history_index += 1
                            buffer = list(_INPUT_HISTORY[history_index])
                        else:
                            history_index = None
                            buffer = list(history_draft)
                        cursor = len(buffer)
                        _redraw_input_line(prompt, buffer, cursor)
                    continue

                if key == "C":
                    if modifier in {"3", "9"}:
                        next_cursor = _move_word_right(buffer, cursor)
                        if next_cursor != cursor:
                            cursor = next_cursor
                            _redraw_input_line(prompt, buffer, cursor)
                    elif cursor < len(buffer):
                        cursor += 1
                        _redraw_input_line(prompt, buffer, cursor)
                    continue

                if key == "D":
                    if modifier in {"3", "9"}:
                        next_cursor = _move_word_left(buffer, cursor)
                        if next_cursor != cursor:
                            cursor = next_cursor
                            _redraw_input_line(prompt, buffer, cursor)
                    elif cursor > 0:
                        cursor -= 1
                        _redraw_input_line(prompt, buffer, cursor)
                    continue

                continue

            if ch.isprintable():
                buffer.insert(cursor, ch)
                cursor += 1
                history_index = None
                _redraw_input_line(prompt, buffer, cursor)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


class AppServerWsRepl:
    def __init__(
        self,
        uri: str,
        approval_policy: str,
        model: str | None,
        cwd: str | None,
        model_provider: str | None,
        auto_approve: bool,
        final_only: bool,
        show_raw_json: bool,
        local_tool_routing: bool,
        local_tool_shell_mode: str,
        local_tool_shell_init: str | None,
        gateway_token: str | None,
        gateway_provider_id: str | None,
        gateway_providers: list[dict[str, Any]] | None,
    ) -> None:
        self.uri = uri
        self.approval_policy = approval_policy
        self.model = model
        self.local_tool_routing = local_tool_routing
        self.local_tool_shell_mode = local_tool_shell_mode
        if isinstance(local_tool_shell_init, str):
            normalized_init = local_tool_shell_init.strip()
            self.local_tool_shell_init = normalized_init or None
        else:
            self.local_tool_shell_init = None
        self.cwd = cwd or (os.getcwd() if local_tool_routing else None)
        self.model_provider = model_provider
        self.auto_approve = auto_approve
        self.final_only = final_only
        self.show_raw_json = show_raw_json
        self.gateway_token = gateway_token.strip() if isinstance(gateway_token, str) else None
        self.gateway_provider_id = (
            gateway_provider_id.strip() if isinstance(gateway_provider_id, str) else None
        )
        self.gateway_providers = gateway_providers or []

        self._ws: Any | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._closed = asyncio.Event()

        self._next_id = 1
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._turn_done = asyncio.Event()
        self._turn_done.set()
        self._ephemeral_status_active = False
        self._ephemeral_status_buffer = ""

        self.active_thread_id: str | None = None
        self.active_turn_id: str | None = None
        self._next_local_session_id = 1
        self._local_exec_sessions: dict[int, LocalExecSession] = {}
        self._persistent_shell_session_id: int | None = None

    def _clear_ephemeral_status(self) -> None:
        if self._ephemeral_status_active:
            sys.stdout.write("\r\x1b[2K")
            sys.stdout.flush()
        self._ephemeral_status_active = False
        self._ephemeral_status_buffer = ""

    def _show_ephemeral_status(self, text: str) -> None:
        compact = format_ephemeral_status_text(text)
        if not compact:
            return
        sys.stdout.write(f"\r\x1b[2K{compact}")
        sys.stdout.flush()
        self._ephemeral_status_active = True

    def _show_ephemeral_status_delta(self, delta: str) -> None:
        self._ephemeral_status_buffer += delta
        if len(self._ephemeral_status_buffer) > 4000:
            self._ephemeral_status_buffer = self._ephemeral_status_buffer[-4000:]
        self._show_ephemeral_status(self._ephemeral_status_buffer)

    def _emit_status_line(self, text: str) -> None:
        self._clear_ephemeral_status()
        _emit_status_line(text)

    async def connect(self) -> None:
        if websockets is None:
            raise RuntimeError("websockets dependency is missing; install `websockets`")
        uri = self.uri
        try:
            connect_kwargs: dict[str, Any] = {
                "compression": None,
                "ping_interval": None,
            }

            if self.gateway_token:
                auth_headers = {"Authorization": f"Bearer {self.gateway_token}"}
                try:
                    self._ws = await websockets.connect(
                        uri,
                        additional_headers=auth_headers,
                        **connect_kwargs,
                    )
                except TypeError:
                    try:
                        self._ws = await websockets.connect(
                            uri,
                            extra_headers=auth_headers,
                            **connect_kwargs,
                        )
                    except TypeError:
                        uri = uri_with_token(uri, self.gateway_token)
                        self._ws = await websockets.connect(uri, **connect_kwargs)
            else:
                self._ws = await websockets.connect(uri, **connect_kwargs)
        except OSError as exc:
            endpoint = format_ws_endpoint(self.uri)
            raise RuntimeError(f"Codex Server nicht erreichbar: {endpoint}") from exc
        self._reader_task = asyncio.create_task(self._reader_loop())

        init_params: dict[str, Any] = {
            "clientInfo": {
                "name": "codex_python_ws_repl",
                "title": "Codex Python WS REPL",
                "version": "0.1.0",
            },
            "capabilities": {
                "experimentalApi": True,
                "optOutNotificationMethods": [
                    "codex/event/agent_message_content_delta",
                    "codex/event/agent_message_delta",
                    "codex/event/item_started",
                    "codex/event/item_completed",
                    "codex/event/token_count",
                    "codex/event/task_started",
                    "codex/event/task_complete",
                    "codex/event/user_message",
                    "account/rateLimits/updated",
                ],
            },
        }
        if self.gateway_providers:
            init_params["xGateway"] = {"providers": self.gateway_providers}

        init_result = await self.request(
            "initialize",
            init_params,
        )
        user_agent = init_result.get("userAgent")
        if user_agent:
            print(f"[init] userAgent={user_agent}")
        if self.local_tool_routing:
            print(
                f"[local-tools] enabled (exec_command/write_stdin on client, mode={self.local_tool_shell_mode})"
            )
        if self.gateway_provider_id:
            print(f"[gateway] default providerId={self.gateway_provider_id}")

        await self.notify("initialized", None)

    async def close(self) -> None:
        await self._close_all_local_sessions()
        if self._ws is not None:
            await self._ws.close()
        if self._reader_task is not None:
            try:
                await self._reader_task
            except Exception as exc:
                ws_exceptions = getattr(websockets, "exceptions", None) if websockets is not None else None
                connection_closed = (
                    getattr(ws_exceptions, "ConnectionClosed", None)
                    if ws_exceptions is not None
                    else None
                )
                if connection_closed is not None and isinstance(exc, connection_closed):
                    return
                raise

    def _new_id(self) -> str:
        value = str(self._next_id)
        self._next_id += 1
        return value

    async def request(self, method: str, params: dict[str, Any] | None) -> dict[str, Any]:
        if self._ws is None:
            raise RuntimeError("websocket is not connected")

        request_id = self._new_id()
        payload: dict[str, Any] = {
            "id": request_id,
            "method": method,
        }
        if params is not None:
            payload["params"] = params

        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[request_id] = fut

        await self._ws.send(json.dumps(payload, ensure_ascii=False))
        response = await fut

        if "error" in response:
            raise RuntimeError(f"{method} failed: {response['error']}")
        return response.get("result", {})

    async def notify(self, method: str, params: dict[str, Any] | None) -> None:
        if self._ws is None:
            raise RuntimeError("websocket is not connected")
        payload: dict[str, Any] = {"method": method}
        if params is not None:
            payload["params"] = params
        await self._ws.send(json.dumps(payload, ensure_ascii=False))

    async def start_thread(self) -> str:
        if self.local_tool_routing and self.local_tool_shell_mode == "persistent":
            await self._close_persistent_shell_if_any()

        params: dict[str, Any] = {
            "approvalPolicy": self.approval_policy,
        }
        if self.model:
            params["model"] = self.model
        if self.cwd:
            params["cwd"] = self.cwd
        if self.model_provider:
            params["modelProvider"] = self.model_provider
        if self.local_tool_routing:
            params["dynamicTools"] = self._local_dynamic_tools()
            params["config"] = {
                "features.shell_tool": False,
            }
            params["developerInstructions"] = self._local_environment_developer_instructions()
        if self.gateway_provider_id:
            params["xGateway"] = {"providerId": self.gateway_provider_id}

        result = await self.request("thread/start", params)
        thread_id = result["thread"]["id"]
        self.active_thread_id = thread_id
        return thread_id

    async def resume_thread(self, thread_id: str) -> str:
        if self.local_tool_routing and self.local_tool_shell_mode == "persistent":
            await self._close_persistent_shell_if_any()

        params: dict[str, Any] = {
            "threadId": thread_id,
            "approvalPolicy": self.approval_policy,
        }
        if self.model:
            params["model"] = self.model
        if self.cwd:
            params["cwd"] = self.cwd
        if self.model_provider:
            params["modelProvider"] = self.model_provider

        result = await self.request("thread/resume", params)
        resumed = result["thread"]["id"]
        self.active_thread_id = resumed
        print(f"[thread] resumed {resumed}")
        if self.local_tool_routing:
            self._emit_status_line(
                "[warn] local tool routing is guaranteed for new threads started via :new"
            )
        return resumed

    async def list_threads(self, limit: int) -> None:
        result = await self.request(
            "thread/list",
            {
                "limit": limit,
                "sortKey": "updated_at",
            },
        )
        data = result.get("data", [])
        if not data:
            print("[threads] no threads")
            return

        print("[threads]")
        for entry in data:
            thread_id = entry.get("id", "?")
            preview = (entry.get("preview") or "").replace("\n", " ")
            print(f"  {thread_id}  {preview[:90]}")

    async def send_turn(self, text: str) -> str:
        if not self.active_thread_id:
            raise RuntimeError("no active thread; use :new or :resume <id>")

        params: dict[str, Any] = {
            "threadId": self.active_thread_id,
            "approvalPolicy": self.approval_policy,
            "input": [
                {
                    "type": "text",
                    "text": text,
                }
            ],
        }
        if self.model:
            params["model"] = self.model
        if self.cwd:
            params["cwd"] = self.cwd
        if self.model_provider:
            params["modelProvider"] = self.model_provider

        self._turn_done.clear()
        try:
            result = await self.request("turn/start", params)
        except Exception:
            self._turn_done.set()
            raise
        turn_id = result["turn"]["id"]
        self.active_turn_id = turn_id
        return turn_id

    async def wait_for_turn_completion(self) -> None:
        await self._turn_done.wait()

    async def interrupt(self, turn_id: str | None) -> None:
        if not self.active_thread_id:
            raise RuntimeError("no active thread")
        chosen = turn_id or self.active_turn_id
        if not chosen:
            raise RuntimeError("no active turn")

        await self.request(
            "turn/interrupt",
            {
                "threadId": self.active_thread_id,
                "turnId": chosen,
            },
        )
        print(f"[turn] interrupt requested for {chosen}")

    async def exec_command(self, command: str) -> None:
        self._clear_ephemeral_status()
        if self.local_tool_routing:
            response = await self._handle_dynamic_exec_command(
                {
                    "cmd": command,
                    "workdir": self.cwd,
                    "login": True,
                    "tty": False,
                    "yield_time_ms": 10000,
                }
            )
            content = self._content_items_to_text(response.get("contentItems") or [])
            if content:
                print(content, end="" if content.endswith("\n") else "\n")
            return

        argv = shlex.split(command)
        if not argv:
            raise RuntimeError("empty command")

        params: dict[str, Any] = {
            "command": argv,
        }
        if self.cwd:
            params["cwd"] = self.cwd

        result = await self.request("command/exec", params)
        exit_code = result.get("exitCode")
        stdout = result.get("stdout", "")
        stderr = result.get("stderr", "")

        if stdout:
            print(stdout, end="" if stdout.endswith("\n") else "\n")
        if stderr:
            print(stderr, end="" if stderr.endswith("\n") else "\n", file=sys.stderr)
        print(f"[exec] exitCode={exit_code}")

    async def _reader_loop(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                if isinstance(raw, bytes):
                    text = raw.decode("utf-8", errors="replace")
                else:
                    text = raw

                try:
                    msg = json.loads(text)
                except json.JSONDecodeError:
                    print(f"[warn] non-json frame: {text!r}")
                    continue

                await self._handle_message(msg)
        except Exception as exc:
            ws_exceptions = getattr(websockets, "exceptions", None) if websockets is not None else None
            connection_closed = (
                getattr(ws_exceptions, "ConnectionClosed", None) if ws_exceptions is not None else None
            )
            if connection_closed is None or not isinstance(exc, connection_closed):
                raise
        finally:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(RuntimeError("connection closed"))
            self._pending.clear()
            self._closed.set()

    async def _handle_message(self, msg: dict[str, Any]) -> None:
        msg_id = msg.get("id")
        method = msg.get("method")

        if msg_id is not None and ("result" in msg or "error" in msg):
            key = str(msg_id)
            future = self._pending.pop(key, None)
            if future is not None and not future.done():
                future.set_result(msg)
            return

        if msg_id is not None and method:
            await self._handle_server_request(msg)
            return

        if method:
            self._handle_notification(method, msg.get("params") or {})
            return

        if not self.final_only:
            print(f"[debug] ignored message: {msg}")

    async def _handle_server_request(self, msg: dict[str, Any]) -> None:
        method = str(msg.get("method"))
        request_id = msg.get("id")

        if method == "item/commandExecution/requestApproval":
            decision = "accept" if self.auto_approve else "decline"
            await self._send_server_response(request_id, {"decision": decision})
            self._emit_status_line(f"[approval] commandExecution -> {decision}")
            return

        if method == "item/fileChange/requestApproval":
            decision = "accept" if self.auto_approve else "decline"
            await self._send_server_response(request_id, {"decision": decision})
            self._emit_status_line(f"[approval] fileChange -> {decision}")
            return

        if method == "item/tool/call":
            params = msg.get("params") or {}
            response = await self._handle_dynamic_tool_call(params)
            await self._send_server_response(request_id, response)
            return

        await self._send_server_error(
            request_id,
            code=-32601,
            message=f"method not supported by client: {method}",
        )
        self._emit_status_line(f"[warn] unsupported server request: {method}")

    async def _send_server_response(self, request_id: Any, result: dict[str, Any]) -> None:
        if self._ws is None:
            return
        await self._ws.send(
            json.dumps(
                {
                    "id": request_id,
                    "result": result,
                },
                ensure_ascii=False,
            )
        )

    async def _send_server_error(self, request_id: Any, code: int, message: str) -> None:
        if self._ws is None:
            return
        await self._ws.send(
            json.dumps(
                {
                    "id": request_id,
                    "error": {
                        "code": code,
                        "message": message,
                    },
                },
                ensure_ascii=False,
            )
        )

    def _handle_notification(self, method: str, params: dict[str, Any]) -> None:
        if method == "item/agentMessage/delta":
            delta = params.get("delta") or ""
            if delta:
                self._clear_ephemeral_status()
                print(format_stream_text(delta), end="", flush=True)
            return

        if method in {"codex/event/agent_message_content_delta", "codex/event/agent_message_delta"}:
            delta = params.get("delta") or params.get("text") or ""
            if delta:
                self._clear_ephemeral_status()
                print(format_stream_text(delta), end="", flush=True)
            return

        if method == "item/commandExecution/outputDelta":
            delta = params.get("delta") or ""
            if delta:
                self._clear_ephemeral_status()
                print(format_stream_text(delta), end="", flush=True)
            return

        if method == "turn/started":
            turn = params.get("turn") or {}
            turn_id = turn.get("id")
            if turn_id:
                self.active_turn_id = turn_id
                self._turn_done.clear()
            if not self.final_only:
                self._emit_status_line(f"[turn] started {turn_id}")
            return

        if method == "turn/completed":
            turn = params.get("turn") or {}
            turn_id = turn.get("id")
            status = turn.get("status")
            if turn_id and turn_id == self.active_turn_id:
                self.active_turn_id = None
            error = turn.get("error")
            if error:
                if not self.final_only:
                    self._emit_status_line(f"[turn] completed id={turn_id} status={status}")
                self._emit_status_line(f"[turn-error] {error.get('message', error)}")
            elif status in {"failed", "interrupted"} and not self.final_only:
                self._emit_status_line(f"[turn] completed id={turn_id} status={status}")
            self._turn_done.set()
            self._emit_status_line("")
            return

        if method == "thread/tokenUsage/updated":
            if self.final_only:
                return
            usage = params.get("tokenUsage") or {}
            total = usage.get("totalTokens")
            if total is not None:
                self._emit_status_line(f"[tokens] total={total}")
            return

        if method == "turn/plan/updated":
            if self.final_only:
                return
            self._emit_status_line("[plan] updated")
            return

        if method == "turn/diff/updated":
            if self.final_only:
                return
            self._emit_status_line("[diff] updated")
            return

        if method == "thread/started":
            thread = params.get("thread") or {}
            thread_id = thread.get("id")
            if thread_id:
                self.active_thread_id = thread_id
            if not self.final_only:
                self._emit_status_line(f"[thread] started {thread_id}")
            return

        if method in {"item/started", "item/completed"}:
            if self.final_only:
                return
            item = params.get("item") or {}
            item_type = item.get("type")
            item_id = item.get("id")
            self._emit_status_line(f"[{method}] type={item_type} id={item_id}")
            return

        if method in {
            "item/plan/delta",
            "item/reasoning/summaryTextDelta",
            "item/reasoning/textDelta",
            "item/fileChange/outputDelta",
        }:
            delta = params.get("delta") or ""
            if delta:
                self._show_ephemeral_status_delta(delta)
            return

        if method == "item/reasoning/summaryPartAdded":
            return

        if method == "codex/event/mcp_startup_update":
            msg = params.get("msg") or {}
            server = msg.get("server")
            status = (msg.get("status") or {}).get("state")
            if server and status and status in {"failed", "error"}:
                self._emit_status_line(f"[mcp] {server}: {status}")
            return

        if method == "codex/event/mcp_startup_complete":
            return

        if method == "account/rateLimits/updated":
            return

        if method.startswith("codex/event/"):
            return

        if method == "error":
            err = params.get("error") or {}
            self._emit_status_line(f"[error] {err.get('message', err)}")
            return

        if self.show_raw_json:
            self._emit_status_line(f"[{method}] {json.dumps(params, ensure_ascii=False)}")
            return

        if not self.final_only:
            self._emit_status_line(f"[{method}]")

    def _local_dynamic_tools(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "exec_command",
                "description": "Runs a command on the connected client machine and returns output or a session ID for ongoing interaction.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "cmd": {"type": "string"},
                        "workdir": {"type": "string"},
                        "shell": {"type": "string"},
                        "login": {"type": "boolean"},
                        "tty": {"type": "boolean"},
                        "yield_time_ms": {"type": "number"},
                        "max_output_tokens": {"type": "number"},
                    },
                    "required": ["cmd"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "write_stdin",
                "description": "Writes characters to an existing client-side exec session and returns recent output.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "session_id": {"type": "number"},
                        "chars": {"type": "string"},
                        "yield_time_ms": {"type": "number"},
                        "max_output_tokens": {"type": "number"},
                    },
                    "required": ["session_id"],
                    "additionalProperties": False,
                },
            },
        ]

    def _local_environment_developer_instructions(self) -> str:
        shell_name = Path(self._default_shell_path()).name
        cwd = self.cwd or os.getcwd()
        os_name = f"{platform.system()} {platform.release()}".strip()
        shell_mode = self.local_tool_shell_mode
        init_hint = (
            f"Client shell init command: {self.local_tool_shell_init}.\n"
            if self.local_tool_shell_init
            else ""
        )
        return (
            "Execution routing: run terminal commands on the connected client machine, not the app-server host.\n"
            f"Client environment: os={os_name}, shell={shell_name}, cwd={cwd}, shell_mode={shell_mode}.\n"
            f"{init_hint}"
            "Use exec_command and write_stdin for shell interactions."
        )

    def _default_shell_path(self) -> str:
        if os.name == "nt":
            return os.environ.get("COMSPEC", "cmd.exe")
        return os.environ.get("SHELL", "/bin/sh")

    def _derive_exec_argv(self, cmd: str, shell: str | None, login: bool) -> list[str]:
        shell_path = shell or self._default_shell_path()
        lower_name = Path(shell_path).name.lower()
        if "powershell" in lower_name or lower_name == "pwsh":
            argv = [shell_path]
            if not login:
                argv.append("-NoProfile")
            argv.extend(["-Command", cmd])
            return argv
        if lower_name in {"cmd", "cmd.exe"}:
            return [shell_path, "/c", cmd]
        arg = "-lc" if login else "-c"
        return [shell_path, arg, cmd]

    def _shell_kind_for_path(self, shell_path: str) -> str:
        lower_name = Path(shell_path).name.lower()
        if "powershell" in lower_name or lower_name == "pwsh":
            return "powershell"
        if lower_name in {"cmd", "cmd.exe"}:
            return "cmd"
        if lower_name in {"csh", "tcsh"}:
            return "csh"
        return "posix"

    def _persistent_shell_argv(self, shell_path: str, login: bool) -> list[str]:
        shell_kind = self._shell_kind_for_path(shell_path)
        if shell_kind == "powershell":
            argv = [shell_path, "-NoLogo"]
            if not login:
                argv.append("-NoProfile")
            argv.append("-NoExit")
            return argv
        if shell_kind == "cmd":
            return [shell_path, "/q", "/k"]
        if shell_kind == "csh":
            return [shell_path, "-l"] if login else [shell_path]
        if login:
            return [shell_path, "-l"]
        return [shell_path]

    def _auto_shell_init_command(self, shell_kind: str, shell_path: str | None) -> str | None:
        if shell_kind == "powershell":
            return "if (Test-Path $PROFILE) { . $PROFILE }"
        if shell_kind == "cmd":
            return None
        if shell_kind == "csh":
            return (
                "if ( -f ~/.cshrc ) source ~/.cshrc; "
                "if ( -f ~/.tcshrc ) source ~/.tcshrc; "
                "if ( -f ~/.login ) source ~/.login"
            )

        lower_name = Path(shell_path or "").name.lower()
        if lower_name == "zsh":
            return "[ -f ~/.zshrc ] && source ~/.zshrc"
        if lower_name in {"bash", "sh"}:
            return "[ -f ~/.bashrc ] && . ~/.bashrc"
        if lower_name in {"ksh", "mksh"}:
            return "[ -f ~/.kshrc ] && . ~/.kshrc"
        return "[ -f ~/.profile ] && . ~/.profile"

    def _command_with_marker(
        self,
        cmd: str,
        workdir: str,
        marker: str,
        shell_kind: str,
    ) -> str:
        if shell_kind == "powershell":
            escaped_workdir = workdir.replace("'", "''")
            return (
                f"Set-Location -LiteralPath '{escaped_workdir}'\n"
                f"{cmd}\n"
                f"$__codex_exit = $LASTEXITCODE\n"
                f"Write-Output \"{marker}:$__codex_exit\"\n"
            )
        if shell_kind == "cmd":
            escaped_workdir = workdir.replace('"', '\\"')
            return (
                f"cd /d \"{escaped_workdir}\"\r\n"
                f"{cmd}\r\n"
                f"echo {marker}:%errorlevel%\r\n"
            )
        if shell_kind == "csh":
            escaped_workdir = workdir.replace("'", "\\'")
            return (
                f"cd '{escaped_workdir}'\n"
                f"{cmd}\n"
                f"echo {marker}:$status\n"
            )

        escaped_workdir = shlex.quote(workdir)
        return (
            f"cd {escaped_workdir}\n"
            f"{cmd}\n"
            f"printf '{marker}:%s\\n' \"$?\"\n"
        )

    async def _ensure_persistent_shell_session(
        self,
        workdir: str,
        shell: str | None,
        login: bool,
    ) -> LocalExecSession:
        if self._persistent_shell_session_id is not None:
            existing = self._local_exec_sessions.get(self._persistent_shell_session_id)
            if existing is not None and existing.process.returncode is None:
                return existing
            self._persistent_shell_session_id = None

        shell_path = shell or self._default_shell_path()
        shell_kind = self._shell_kind_for_path(shell_path)
        argv = self._persistent_shell_argv(shell_path, login)

        process: asyncio.subprocess.Process
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=workdir,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except Exception as exc:
            raise RuntimeError(f"persistent shell failed to start: {exc}") from exc

        session = self._register_local_session(
            process,
            persistent_shell=True,
            shell_path=shell_path,
            shell_kind=shell_kind,
            login_shell=login,
        )
        self._persistent_shell_session_id = session.session_id

        init_command = self.local_tool_shell_init
        if init_command == "auto":
            init_command = self._auto_shell_init_command(session.shell_kind, session.shell_path)

        if init_command:
            marker = f"__CODEX_INIT_{uuid.uuid4().hex}__"
            payload = self._command_with_marker(
                init_command,
                workdir,
                marker,
                session.shell_kind,
            )
            stdin = session.process.stdin
            if stdin is None or stdin.is_closing():
                await self._finalize_local_session(session.session_id)
                raise RuntimeError("persistent shell stdin is closed during init")
            stdin.write(payload.encode("utf-8", errors="replace"))
            with contextlib.suppress(Exception):
                await stdin.drain()
            session.active_marker = marker
            session.active_started_at = asyncio.get_running_loop().time()

            output, running, exit_code = await self._collect_persistent_command_output(
                session=session,
                yield_time_ms=10000,
                max_output_tokens=2000,
            )
            if running:
                await self._finalize_local_session(session.session_id)
                raise RuntimeError("local shell init did not complete within 10 seconds")
            if exit_code not in {None, 0}:
                await self._finalize_local_session(session.session_id)
                error_text = output.strip()
                if error_text:
                    raise RuntimeError(f"local shell init failed: {error_text}")
                raise RuntimeError(f"local shell init failed with exit code {exit_code}")

        return session

    async def _collect_persistent_command_output(
        self,
        session: LocalExecSession,
        yield_time_ms: int,
        max_output_tokens: int | None,
    ) -> tuple[str, bool, int | None]:
        marker = session.active_marker
        if marker is None:
            output, _ = await self._collect_session_output(
                session=session,
                yield_time_ms=yield_time_ms,
                max_output_tokens=max_output_tokens,
            )
            running = session.process.returncode is None
            return output, running, session.process.returncode

        marker_regex = re.compile(re.escape(f"{marker}:") + r"(-?\d+)\r?\n")
        timeout_seconds = max(yield_time_ms, 0) / 1000.0
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        chunks: list[str] = []
        exit_code: int | None = None

        while True:
            match = marker_regex.search(session.pending_buffer)
            if match:
                chunks.append(session.pending_buffer[: match.start()])
                session.pending_buffer = session.pending_buffer[match.end() :]
                exit_code = int(match.group(1))
                session.active_marker = None
                session.active_started_at = None
                break

            if session.process.returncode is not None and session.output_queue.empty():
                chunks.append(session.pending_buffer)
                session.pending_buffer = ""
                session.active_marker = None
                session.active_started_at = None
                exit_code = session.process.returncode
                break

            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break

            try:
                chunk = await asyncio.wait_for(session.output_queue.get(), timeout=remaining)
            except TimeoutError:
                break
            session.pending_buffer += chunk

            while True:
                try:
                    session.pending_buffer += session.output_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

            if len(session.pending_buffer) > 8192:
                chunks.append(session.pending_buffer[:-8192])
                session.pending_buffer = session.pending_buffer[-8192:]

        output = "".join(chunks)
        original_token_count = max(1, len(output) // 4) if output else 0
        if max_output_tokens is not None and max_output_tokens >= 0:
            max_chars = max_output_tokens * 4
            if len(output) > max_chars:
                output = output[:max_chars]

        running = exit_code is None and session.process.returncode is None
        if running:
            return output, True, None
        return output, False, exit_code

    async def _handle_dynamic_tool_call(self, params: dict[str, Any]) -> dict[str, Any]:
        if not self.local_tool_routing:
            return self._dynamic_tool_failure("Tool call not handled by this client.")

        tool = str(params.get("tool") or "")
        arguments_raw = params.get("arguments")
        arguments = arguments_raw if isinstance(arguments_raw, dict) else {}

        if tool == "exec_command":
            return await self._handle_dynamic_exec_command(arguments)
        if tool == "write_stdin":
            return await self._handle_dynamic_write_stdin(arguments)
        return self._dynamic_tool_failure(f"Unsupported dynamic tool: {tool}")

    def _dynamic_tool_failure(self, text: str) -> dict[str, Any]:
        return {
            "contentItems": [{"type": "inputText", "text": text}],
            "success": False,
        }

    def _dynamic_tool_success(self, text: str) -> dict[str, Any]:
        return {
            "contentItems": [{"type": "inputText", "text": text}],
            "success": True,
        }

    def _content_items_to_text(self, content_items: list[dict[str, Any]]) -> str:
        parts: list[str] = []
        for item in content_items:
            if item.get("type") == "inputText":
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)

    async def _handle_dynamic_exec_command(self, arguments: dict[str, Any]) -> dict[str, Any]:
        cmd = arguments.get("cmd")
        if not isinstance(cmd, str) or not cmd.strip():
            return self._dynamic_tool_failure("exec_command requires a non-empty 'cmd' string.")

        workdir_value = arguments.get("workdir")
        workdir = (
            str(workdir_value)
            if isinstance(workdir_value, str) and workdir_value.strip()
            else (self.cwd or os.getcwd())
        )
        shell = arguments.get("shell")
        shell_value = shell if isinstance(shell, str) and shell.strip() else None
        login = bool(arguments.get("login", True))
        tty_requested = bool(arguments.get("tty", False))
        yield_time_ms = int(arguments.get("yield_time_ms", 10000))
        max_output_tokens_raw = arguments.get("max_output_tokens")
        max_output_tokens = (
            int(max_output_tokens_raw) if isinstance(max_output_tokens_raw, (int, float)) else None
        )

        if self.local_tool_shell_mode == "persistent":
            try:
                session = await self._ensure_persistent_shell_session(
                    workdir=workdir,
                    shell=shell_value,
                    login=login,
                )
            except RuntimeError as exc:
                return self._dynamic_tool_failure(str(exc))

            async with session.command_lock:
                if session.active_marker is not None:
                    return self._dynamic_tool_failure(
                        f"Previous command still running in session_id {session.session_id}; use write_stdin."
                    )
                marker = f"__CODEX_EXIT_{uuid.uuid4().hex}__"
                payload = self._command_with_marker(cmd, workdir, marker, session.shell_kind)
                stdin = session.process.stdin
                if stdin is None or stdin.is_closing():
                    return self._dynamic_tool_failure("persistent shell stdin is closed")
                stdin.write(payload.encode("utf-8", errors="replace"))
                with contextlib.suppress(Exception):
                    await stdin.drain()
                session.active_marker = marker
                session.active_started_at = asyncio.get_running_loop().time()
                command_started_at = session.active_started_at

                output, running, exit_code = await self._collect_persistent_command_output(
                    session=session,
                    yield_time_ms=yield_time_ms,
                    max_output_tokens=max_output_tokens,
                )
                if not running and session.process.returncode is not None:
                    await self._finalize_local_session(session.session_id)

            text = self._format_dynamic_exec_output(
                session=session,
                output=output,
                include_session_id=running,
                tty_requested=tty_requested,
                explicit_exit_code=exit_code,
                started_at_override=command_started_at,
            )
            return self._dynamic_tool_success(text)

        argv = self._derive_exec_argv(cmd, shell_value, login)
        process: asyncio.subprocess.Process
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=workdir,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except Exception as exc:
            return self._dynamic_tool_failure(f"exec_command failed to start: {exc}")

        session = self._register_local_session(process)
        output, _original_token_count = await self._collect_session_output(
            session=session,
            yield_time_ms=yield_time_ms,
            max_output_tokens=max_output_tokens,
        )
        running = not session.completion_event.is_set()
        if not running and (not session.persistent_shell or session.process.returncode is not None):
            await self._finalize_local_session(session.session_id)

        text = self._format_dynamic_exec_output(
            session=session,
            output=output,
            include_session_id=running,
            tty_requested=tty_requested,
            explicit_exit_code=None,
            started_at_override=None,
        )
        return self._dynamic_tool_success(text)

    async def _handle_dynamic_write_stdin(self, arguments: dict[str, Any]) -> dict[str, Any]:
        session_id_raw = arguments.get("session_id")
        if not isinstance(session_id_raw, (int, float)):
            return self._dynamic_tool_failure("write_stdin requires numeric 'session_id'.")
        session_id = int(session_id_raw)
        session = self._local_exec_sessions.get(session_id)
        if session is None:
            return self._dynamic_tool_failure(f"Unknown session_id: {session_id}")

        chars = arguments.get("chars", "")
        if not isinstance(chars, str):
            return self._dynamic_tool_failure("write_stdin 'chars' must be a string.")
        yield_time_ms = int(arguments.get("yield_time_ms", 250))
        max_output_tokens_raw = arguments.get("max_output_tokens")
        max_output_tokens = (
            int(max_output_tokens_raw) if isinstance(max_output_tokens_raw, (int, float)) else None
        )

        stdin = session.process.stdin
        if chars and stdin is not None and not stdin.is_closing():
            stdin.write(chars.encode("utf-8", errors="replace"))
            with contextlib.suppress(Exception):
                await stdin.drain()

        if session.persistent_shell:
            output, running, exit_code = await self._collect_persistent_command_output(
                session=session,
                yield_time_ms=yield_time_ms,
                max_output_tokens=max_output_tokens,
            )
        else:
            output, _ = await self._collect_session_output(
                session=session,
                yield_time_ms=yield_time_ms,
                max_output_tokens=max_output_tokens,
            )
            running = not session.completion_event.is_set()
            exit_code = None

        if not running and (not session.persistent_shell or session.process.returncode is not None):
            await self._finalize_local_session(session.session_id)

        text = self._format_dynamic_exec_output(
            session=session,
            output=output,
            include_session_id=running,
            tty_requested=False,
            explicit_exit_code=exit_code,
            started_at_override=None,
        )
        return self._dynamic_tool_success(text)

    def _register_local_session(
        self,
        process: asyncio.subprocess.Process,
        persistent_shell: bool = False,
        shell_path: str | None = None,
        shell_kind: str = "posix",
        login_shell: bool = True,
    ) -> LocalExecSession:
        session_id = self._next_local_session_id
        self._next_local_session_id += 1

        output_queue: asyncio.Queue[str] = asyncio.Queue()
        completion_event = asyncio.Event()

        async def stream_reader(stream: asyncio.StreamReader | None) -> None:
            if stream is None:
                return
            while True:
                chunk = await stream.read(4096)
                if not chunk:
                    return
                text = chunk.decode("utf-8", errors="replace")
                await output_queue.put(text)

        async def wait_for_exit() -> None:
            with contextlib.suppress(Exception):
                await process.wait()
            completion_event.set()

        reader_tasks = [
            asyncio.create_task(stream_reader(process.stdout)),
            asyncio.create_task(stream_reader(process.stderr)),
        ]
        waiter_task = asyncio.create_task(wait_for_exit())

        session = LocalExecSession(
            session_id=session_id,
            process=process,
            output_queue=output_queue,
            started_at=asyncio.get_running_loop().time(),
            completion_event=completion_event,
            reader_tasks=reader_tasks,
            waiter_task=waiter_task,
            persistent_shell=persistent_shell,
            shell_path=shell_path,
            shell_kind=shell_kind,
            login_shell=login_shell,
        )
        self._local_exec_sessions[session_id] = session
        return session

    async def _collect_session_output(
        self,
        session: LocalExecSession,
        yield_time_ms: int,
        max_output_tokens: int | None,
    ) -> tuple[str, int]:
        loop = asyncio.get_running_loop()
        timeout_seconds = max(yield_time_ms, 0) / 1000.0
        deadline = loop.time() + timeout_seconds
        chunks: list[str] = []

        while True:
            if session.completion_event.is_set() and session.output_queue.empty():
                break
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            try:
                chunk = await asyncio.wait_for(session.output_queue.get(), timeout=remaining)
            except TimeoutError:
                break
            chunks.append(chunk)
            while True:
                try:
                    chunks.append(session.output_queue.get_nowait())
                except asyncio.QueueEmpty:
                    break

        output = "".join(chunks)
        original_token_count = max(1, len(output) // 4) if output else 0
        if max_output_tokens is not None and max_output_tokens >= 0:
            max_chars = max_output_tokens * 4
            if len(output) > max_chars:
                output = output[:max_chars]
        return output, original_token_count

    def _format_dynamic_exec_output(
        self,
        session: LocalExecSession,
        output: str,
        include_session_id: bool,
        tty_requested: bool,
        explicit_exit_code: int | None,
        started_at_override: float | None,
    ) -> str:
        started_at = started_at_override or session.started_at
        wall_time_seconds = asyncio.get_running_loop().time() - started_at
        sections = [
            f"Chunk ID: local-{session.session_id}-{uuid.uuid4().hex[:6]}",
            f"Wall time: {wall_time_seconds:.4f} seconds",
        ]
        if include_session_id:
            sections.append(f"Process running with session ID {session.session_id}")
        else:
            exit_code = explicit_exit_code if explicit_exit_code is not None else session.process.returncode
            if exit_code is not None:
                sections.append(f"Process exited with code {exit_code}")
        if tty_requested:
            sections.append("TTY requested but not supported by this client; used pipes instead.")
        sections.append("Output:")
        sections.append(output)
        return "\n".join(sections)

    async def _finalize_local_session(self, session_id: int) -> None:
        session = self._local_exec_sessions.pop(session_id, None)
        if session is None:
            return
        if self._persistent_shell_session_id == session_id:
            self._persistent_shell_session_id = None

        for task in session.reader_tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        with contextlib.suppress(asyncio.CancelledError):
            session.waiter_task.cancel()
            await session.waiter_task

    async def _close_all_local_sessions(self) -> None:
        for session_id in list(self._local_exec_sessions.keys()):
            session = self._local_exec_sessions.get(session_id)
            if session is None:
                continue
            if session.process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    session.process.terminate()
                try:
                    await asyncio.wait_for(session.process.wait(), timeout=1.0)
                except TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        session.process.kill()
                    with contextlib.suppress(Exception):
                        await session.process.wait()
            await self._finalize_local_session(session_id)

    async def _close_persistent_shell_if_any(self) -> None:
        session_id = self._persistent_shell_session_id
        if session_id is None:
            return
        session = self._local_exec_sessions.get(session_id)
        if session is None:
            self._persistent_shell_session_id = None
            return
        if session.process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                session.process.terminate()
            try:
                await asyncio.wait_for(session.process.wait(), timeout=1.0)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    session.process.kill()
                with contextlib.suppress(Exception):
                    await session.process.wait()
        await self._finalize_local_session(session_id)


def print_help() -> None:
    print(
        """
Commands:
  :help                     Show this help
  :new                      Start a new thread
  :resume <thread-id>       Resume thread and switch active thread
  :use <thread-id>          Switch active thread locally (no RPC)
  :threads [limit]          List recent threads (default 20)
  :interrupt [turn-id]      Interrupt active turn (or explicit turn id)
  :exec <shell command>     Run local exec on this client (or server if local routing is disabled)
  :quit                     Exit

Any line without ':' is sent as a new user turn (turn/start).
Line editing: Arrow keys navigate history/cursor, Option+Left/Right jumps by word, Ctrl+X clears current input line.
""".strip()
    )


async def run_repl(args: argparse.Namespace) -> int:
    configure_readline()

    repl = AppServerWsRepl(
        uri=args.url,
        approval_policy=args.approval_policy,
        model=args.model,
        cwd=args.cwd,
        model_provider=args.model_provider,
        auto_approve=args.auto_approve,
        final_only=args.final_only,
        show_raw_json=args.show_raw_json,
        local_tool_routing=args.local_tool_routing,
        local_tool_shell_mode=args.local_tool_shell_mode,
        local_tool_shell_init=args.local_tool_shell_init,
        gateway_token=args.token,
        gateway_provider_id=args.provider_id,
        gateway_providers=load_gateway_providers(args.providers_json),
    )
    await repl.connect()
    try:
        if args.thread_id:
            await repl.resume_thread(args.thread_id)
        else:
            await repl.start_thread()

        print_help()

        while True:
            line = await asyncio.to_thread(read_user_input, "> ")
            if not line.strip():
                continue

            command = parse_repl_command(line)

            if command.kind == "help":
                print_help()
                continue
            if command.kind == "quit":
                return 0
            if command.kind == "new":
                await repl.start_thread()
                continue
            if command.kind == "resume":
                if not command.arg:
                    print("usage: :resume <thread-id>")
                    continue
                await repl.resume_thread(command.arg)
                continue
            if command.kind == "use":
                if not command.arg:
                    print("usage: :use <thread-id>")
                    continue
                repl.active_thread_id = command.arg
                print(f"[thread] switched to {command.arg}")
                continue
            if command.kind == "threads":
                limit_text = (command.arg or "20").strip()
                try:
                    limit = int(limit_text)
                except ValueError:
                    print("usage: :threads [limit]")
                    continue
                await repl.list_threads(limit)
                continue
            if command.kind == "interrupt":
                await repl.interrupt(command.arg.strip() if command.arg else None)
                continue
            if command.kind == "exec":
                if not command.arg:
                    print("usage: :exec <shell command>")
                    continue
                await repl.exec_command(command.arg)
                continue
            if command.kind == "unknown":
                print(f"unknown command: :{command.arg}")
                continue

            await repl.send_turn(command.arg or "")
            await repl.wait_for_turn_completion()

    finally:
        await repl.close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Remote interactive client for codex app-server over WebSocket",
    )
    parser.add_argument(
        "--url",
        default="ws://127.0.0.1:4222",
        help="app-server WebSocket URL",
    )
    parser.add_argument(
        "--thread-id",
        default=None,
        help="resume this thread on startup",
    )
    parser.add_argument(
        "--approval-policy",
        choices=["untrusted", "on-failure", "on-request", "never"],
        default="never",
        help="approval policy for new/resumed threads and turns",
    )
    parser.add_argument(
        "--auto-approve",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="auto-respond accept/decline to approval requests",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="optional model override",
    )
    parser.add_argument(
        "--model-provider",
        default=None,
        help="optional model provider override",
    )
    parser.add_argument(
        "--cwd",
        default=None,
        help="optional cwd override for thread/turn/exec",
    )
    parser.add_argument(
        "--final-only",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="show fewer metadata notifications",
    )
    parser.add_argument(
        "--show-raw-json",
        action="store_true",
        help="print full JSON params for notifications (debug)",
    )
    parser.add_argument(
        "--local-tool-routing",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="route exec_command/write_stdin tool calls to this client machine",
    )
    parser.add_argument(
        "--local-tool-shell-mode",
        choices=["subprocess", "persistent"],
        default="subprocess",
        help="local tool execution mode: one shell per command (subprocess) or one persistent shell",
    )
    parser.add_argument(
        "--local-tool-shell-init",
        default="auto",
        help=(
            "optional shell init command for persistent mode; "
            "use 'auto' (default) for shell-specific rc loading, empty string to disable"
        ),
    )
    parser.add_argument(
        "--token",
        default=None,
        help="optional gateway bearer token",
    )
    parser.add_argument(
        "--provider-id",
        default=None,
        help="optional xGateway providerId for thread/start",
    )
    parser.add_argument(
        "--providers-json",
        default=None,
        help=(
            "optional JSON array for initialize.params.xGateway.providers; "
            "use @/path/to/providers.json to read from file"
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return asyncio.run(run_repl(args))
    except KeyboardInterrupt:
        return 130
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1
    except Exception as exc:
        print(exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
