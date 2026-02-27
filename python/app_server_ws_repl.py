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
from dataclasses import dataclass
import json
from pathlib import Path
import re
import shlex
import sys
import termios
import tty
from typing import Any
from urllib.parse import urlparse

try:
    import websockets
except Exception:  # pragma: no cover - optional for parser-only tests
    websockets = None


@dataclass
class ReplCommand:
    kind: str
    arg: str | None = None


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
    ) -> None:
        self.uri = uri
        self.approval_policy = approval_policy
        self.model = model
        self.cwd = cwd
        self.model_provider = model_provider
        self.auto_approve = auto_approve
        self.final_only = final_only
        self.show_raw_json = show_raw_json

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
        try:
            self._ws = await websockets.connect(
                self.uri,
                compression=None,
                ping_interval=None,
            )
        except OSError as exc:
            endpoint = format_ws_endpoint(self.uri)
            raise RuntimeError(f"Codex Server nicht erreichbar: {endpoint}") from exc
        self._reader_task = asyncio.create_task(self._reader_loop())

        init_result = await self.request(
            "initialize",
            {
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
            },
        )
        user_agent = init_result.get("userAgent")
        if user_agent:
            print(f"[init] userAgent={user_agent}")

        await self.notify("initialized", None)

    async def close(self) -> None:
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
        params: dict[str, Any] = {
            "approvalPolicy": self.approval_policy,
        }
        if self.model:
            params["model"] = self.model
        if self.cwd:
            params["cwd"] = self.cwd
        if self.model_provider:
            params["modelProvider"] = self.model_provider

        result = await self.request("thread/start", params)
        thread_id = result["thread"]["id"]
        self.active_thread_id = thread_id
        return thread_id

    async def resume_thread(self, thread_id: str) -> str:
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
            await self._send_server_response(
                request_id,
                {
                    "contentItems": [
                        {
                            "type": "inputText",
                            "text": "Tool call not handled by this client.",
                        }
                    ],
                    "success": False,
                },
            )
            self._emit_status_line("[tool] item/tool/call declined (not implemented)")
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
  :exec <shell command>     Run command/exec directly on server
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
