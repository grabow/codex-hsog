#!/usr/bin/env python3
"""Codex Rust GUI client with live streaming output.

Features:
- Prompt control window (send prompt to `codex exec`).
- Separate stream window that shows live output chunks.
- Optional WebSocket mode (send prompt to TUI WS and stream replies).
- Headless mode for scripted integration tests.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading
import time
from typing import Callable
from typing import Optional

try:
    import tkinter as tk
    from tkinter import messagebox
    from tkinter import scrolledtext
    from tkinter import ttk
except Exception:  # pragma: no cover - tkinter availability depends on runtime
    tk = None
    messagebox = None
    scrolledtext = None
    ttk = None

try:
    import websockets
except Exception:  # pragma: no cover - optional in exec-only mode
    websockets = None


DEFAULT_WS_URI = "ws://127.0.0.1:8765"
DEFAULT_RECONNECT_DELAY = 2.0


def build_ws_prompt_payload(prompt: str) -> str:
    """Encode prompt text as websocket payload accepted by codex-rs."""
    return json.dumps({"text": prompt}, ensure_ascii=False)


@dataclass
class StreamEvent:
    source: str
    text: str
    timestamp: datetime


def detect_codex_binary(preferred: Optional[str] = None) -> str:
    """Return the most likely codex binary path available on this machine."""
    candidates: list[str] = []
    if preferred:
        candidates.append(preferred)

    env_candidate = os.environ.get("CODEX_BINARY")
    if env_candidate:
        candidates.append(env_candidate)

    candidates.extend(
        [
            "codex-rust",
            "codex",
            "./codex-rs/target/debug/codex",
            "./bazel-bin/codex-rs/cli/codex",
        ]
    )

    for candidate in candidates:
        resolved = shutil.which(candidate)
        if resolved:
            return resolved

        path = Path(candidate)
        if path.exists() and os.access(path, os.X_OK):
            return str(path)

    return preferred or "codex"


def build_exec_command(
    binary: str,
    prompt: str,
    profile: Optional[str],
    model: Optional[str],
    cwd_override: Optional[str],
    skip_git_repo_check: bool,
    ephemeral: bool,
    extra_args: list[str],
) -> list[str]:
    """Build the `codex exec ...` command line."""
    cmd = [binary, "exec"]

    if profile:
        cmd.extend(["-p", profile])

    if model:
        cmd.extend(["-m", model])

    if cwd_override:
        cmd.extend(["-C", cwd_override])

    if skip_git_repo_check:
        cmd.append("--skip-git-repo-check")

    if ephemeral:
        cmd.append("--ephemeral")

    cmd.extend(extra_args)
    cmd.append(prompt)
    return cmd


def _read_pipe(pipe: Optional[object], source: str, emit: Callable[[StreamEvent], None]) -> None:
    if pipe is None:
        return

    fileno = pipe.fileno()
    while True:
        chunk = os.read(fileno, 4096)
        if not chunk:
            break
        text = chunk.decode("utf-8", errors="replace")
        emit(StreamEvent(source=source, text=text, timestamp=datetime.now()))


class ExecRunner:
    """Run one `codex exec` process and stream stdout/stderr events."""

    def __init__(self, emit: Callable[[StreamEvent], None]) -> None:
        self._emit = emit
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._process: Optional[subprocess.Popen[bytes]] = None

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, command: list[str], working_dir: str) -> bool:
        with self._lock:
            if self.is_running():
                return False

            self._thread = threading.Thread(
                target=self._run,
                args=(command, working_dir),
                daemon=True,
            )
            self._thread.start()
            return True

    def stop(self) -> None:
        with self._lock:
            if self._process and self._process.poll() is None:
                self._emit(
                    StreamEvent(
                        source="status",
                        text="Stopping running process...\n",
                        timestamp=datetime.now(),
                    )
                )
                self._process.terminate()

    def _run(self, command: list[str], working_dir: str) -> None:
        self._emit(
            StreamEvent(
                source="status",
                text=f"Running: {' '.join(command)}\n",
                timestamp=datetime.now(),
            )
        )

        try:
            process = subprocess.Popen(
                command,
                cwd=working_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                text=False,
                bufsize=0,
            )
        except Exception as exc:
            self._emit(
                StreamEvent(
                    source="status",
                    text=f"Failed to start process: {exc}\n",
                    timestamp=datetime.now(),
                )
            )
            return

        with self._lock:
            self._process = process

        stdout_thread = threading.Thread(
            target=_read_pipe,
            args=(process.stdout, "stdout", self._emit),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=_read_pipe,
            args=(process.stderr, "stderr", self._emit),
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()

        return_code = process.wait()
        stdout_thread.join(timeout=1.0)
        stderr_thread.join(timeout=1.0)

        with self._lock:
            self._process = None

        self._emit(
            StreamEvent(
                source="status",
                text=f"Process exit code: {return_code}\n",
                timestamp=datetime.now(),
            )
        )


class WebSocketMirror:
    """Mirror TUI WebSocket output into StreamEvent messages."""

    def __init__(
        self,
        emit: Callable[[StreamEvent], None],
        reconnect_delay: float = DEFAULT_RECONNECT_DELAY,
    ) -> None:
        self._emit = emit
        self._reconnect_delay = reconnect_delay
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, uri: str) -> bool:
        if websockets is None:
            self._emit(
                StreamEvent(
                    source="status",
                    text="WebSocket mode unavailable: install `websockets`.\n",
                    timestamp=datetime.now(),
                )
            )
            return False

        if self.is_running():
            return False

        self._stop.clear()
        self._thread = threading.Thread(target=self._run, args=(uri,), daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()

    def _run(self, uri: str) -> None:
        asyncio.run(self._run_async(uri))

    async def _run_async(self, uri: str) -> None:
        while not self._stop.is_set():
            try:
                self._emit(
                    StreamEvent(
                        source="status",
                        text=f"Connecting WS: {uri}\n",
                        timestamp=datetime.now(),
                    )
                )
                async with websockets.connect(
                    uri,
                    compression=None,
                    ping_interval=None,
                ) as ws:
                    self._emit(
                        StreamEvent(
                            source="status",
                            text="WS connected.\n",
                            timestamp=datetime.now(),
                        )
                    )

                    while not self._stop.is_set():
                        try:
                            message = await asyncio.wait_for(ws.recv(), timeout=0.2)
                        except asyncio.TimeoutError:
                            continue

                        if isinstance(message, bytes):
                            text = message.decode("utf-8", errors="replace")
                        else:
                            text = message

                        self._emit(
                            StreamEvent(
                                source="ws",
                                text=text,
                                timestamp=datetime.now(),
                            )
                        )

            except Exception as exc:
                self._emit(
                    StreamEvent(
                        source="status",
                        text=f"WS disconnected/error: {exc}\n",
                        timestamp=datetime.now(),
                    )
                )

            sleep_until = time.monotonic() + self._reconnect_delay
            while not self._stop.is_set() and time.monotonic() < sleep_until:
                await asyncio.sleep(0.05)

        self._emit(
            StreamEvent(
                source="status",
                text="WS mirror stopped.\n",
                timestamp=datetime.now(),
            )
        )


class WebSocketWriter:
    """Send prompts to the Codex TUI websocket endpoint."""

    def __init__(self, emit: Callable[[StreamEvent], None]) -> None:
        self._emit = emit

    def send_prompt(self, uri: str, prompt: str) -> None:
        if websockets is None:
            self._emit(
                StreamEvent(
                    source="status",
                    text="WebSocket write unavailable: install `websockets`.\n",
                    timestamp=datetime.now(),
                )
            )
            return

        thread = threading.Thread(
            target=self._run,
            args=(uri, prompt),
            daemon=True,
        )
        thread.start()

    def _run(self, uri: str, prompt: str) -> None:
        asyncio.run(self._run_async(uri, prompt))

    async def _run_async(self, uri: str, prompt: str) -> None:
        try:
            async with websockets.connect(
                uri,
                compression=None,
                ping_interval=None,
            ) as ws:
                payload = build_ws_prompt_payload(prompt)
                await ws.send(payload)
                self._emit(
                    StreamEvent(
                        source="status",
                        text=f"WS prompt sent ({len(prompt)} chars).\n",
                        timestamp=datetime.now(),
                    )
                )
        except Exception as exc:
            self._emit(
                StreamEvent(
                    source="status",
                    text=f"WS send failed: {exc}\n",
                    timestamp=datetime.now(),
                )
            )


class CodexGuiClient:
    """Tkinter UI with control window + separate stream window."""

    def __init__(self, args: argparse.Namespace) -> None:
        if tk is None or ttk is None or scrolledtext is None:
            raise RuntimeError("Tkinter is not available in this Python environment")

        self.args = args
        self.root = tk.Tk()
        self.root.title("Codex Rust Client Control")
        self.root.geometry("920x700")

        self.output_window = tk.Toplevel(self.root)
        self.output_window.title("Codex Rust Stream")
        self.output_window.geometry("1100x720")

        self.event_queue: "queue.Queue[StreamEvent]" = queue.Queue()
        self.runner = ExecRunner(self._enqueue)
        self.ws_mirror = WebSocketMirror(self._enqueue, reconnect_delay=args.reconnect_delay)
        self.ws_writer = WebSocketWriter(self._enqueue)

        default_binary = detect_codex_binary(args.binary)
        default_cwd = args.cwd or str(Path.cwd())

        self.binary_var = tk.StringVar(value=default_binary)
        self.profile_var = tk.StringVar(value=args.profile)
        self.model_var = tk.StringVar(value=args.model)
        self.cwd_var = tk.StringVar(value=default_cwd)
        self.ws_uri_var = tk.StringVar(value=args.ws_uri)
        self.mode_var = tk.StringVar(value=args.mode)
        self.skip_git_var = tk.BooleanVar(value=args.skip_git_repo_check)
        self.ephemeral_var = tk.BooleanVar(value=args.ephemeral)
        self.show_timestamps_var = tk.BooleanVar(value=True)

        self._build_control_ui()
        self._build_output_ui()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.output_window.protocol("WM_DELETE_WINDOW", self._on_close)

        self._append_status("GUI ready.")
        self.root.after(50, self._drain_events)

    def _enqueue(self, event: StreamEvent) -> None:
        self.event_queue.put(event)

    def _append_status(self, text: str) -> None:
        suffix = "" if text.endswith("\n") else "\n"
        self._enqueue(
            StreamEvent(source="status", text=f"{text}{suffix}", timestamp=datetime.now())
        )

    def _build_control_ui(self) -> None:
        frame = ttk.Frame(self.root, padding=12)
        frame.pack(fill=tk.BOTH, expand=True)

        fields = ttk.LabelFrame(frame, text="Connection + Execution", padding=8)
        fields.pack(fill=tk.X, pady=(0, 10))

        fields.columnconfigure(1, weight=1)

        ttk.Label(fields, text="Codex binary").grid(row=0, column=0, sticky=tk.W, padx=4, pady=4)
        ttk.Entry(fields, textvariable=self.binary_var).grid(
            row=0, column=1, sticky=tk.EW, padx=4, pady=4
        )

        ttk.Label(fields, text="Profile (-p)").grid(row=1, column=0, sticky=tk.W, padx=4, pady=4)
        ttk.Entry(fields, textvariable=self.profile_var).grid(
            row=1, column=1, sticky=tk.EW, padx=4, pady=4
        )

        ttk.Label(fields, text="Model (-m)").grid(row=2, column=0, sticky=tk.W, padx=4, pady=4)
        ttk.Entry(fields, textvariable=self.model_var).grid(
            row=2, column=1, sticky=tk.EW, padx=4, pady=4
        )

        ttk.Label(fields, text="Working dir").grid(row=3, column=0, sticky=tk.W, padx=4, pady=4)
        ttk.Entry(fields, textvariable=self.cwd_var).grid(
            row=3, column=1, sticky=tk.EW, padx=4, pady=4
        )

        ttk.Label(fields, text="WS URI").grid(row=4, column=0, sticky=tk.W, padx=4, pady=4)
        ttk.Entry(fields, textvariable=self.ws_uri_var).grid(
            row=4, column=1, sticky=tk.EW, padx=4, pady=4
        )

        ttk.Label(fields, text="Mode").grid(row=5, column=0, sticky=tk.W, padx=4, pady=4)
        mode_combo = ttk.Combobox(
            fields,
            textvariable=self.mode_var,
            values=["exec", "ws", "both"],
            state="readonly",
        )
        mode_combo.grid(row=5, column=1, sticky=tk.W, padx=4, pady=4)

        toggles = ttk.Frame(fields)
        toggles.grid(row=6, column=0, columnspan=2, sticky=tk.W, padx=4, pady=(4, 2))
        ttk.Checkbutton(
            toggles,
            text="--skip-git-repo-check",
            variable=self.skip_git_var,
        ).pack(side=tk.LEFT, padx=(0, 16))
        ttk.Checkbutton(
            toggles,
            text="--ephemeral",
            variable=self.ephemeral_var,
        ).pack(side=tk.LEFT)

        controls = ttk.Frame(frame)
        controls.pack(fill=tk.X, pady=(0, 10))

        ttk.Button(controls, text="Send Prompt", command=self._on_send_prompt).pack(
            side=tk.LEFT, padx=(0, 8)
        )
        ttk.Button(controls, text="Start WS", command=self._on_start_ws).pack(side=tk.LEFT, padx=8)
        ttk.Button(controls, text="Stop WS", command=self._on_stop_ws).pack(side=tk.LEFT, padx=8)
        ttk.Button(controls, text="Stop Exec", command=self._on_stop_exec).pack(side=tk.LEFT, padx=8)
        ttk.Button(controls, text="Clear Stream", command=self._clear_output).pack(side=tk.LEFT, padx=8)

        prompt_frame = ttk.LabelFrame(frame, text="Prompt", padding=8)
        prompt_frame.pack(fill=tk.BOTH, expand=True)

        self.prompt_text = scrolledtext.ScrolledText(
            prompt_frame,
            wrap=tk.WORD,
            height=16,
            font=("Menlo", 12),
        )
        self.prompt_text.pack(fill=tk.BOTH, expand=True)

        history_frame = ttk.LabelFrame(frame, text="Prompt History", padding=8)
        history_frame.pack(fill=tk.BOTH, expand=True, pady=(10, 0))

        self.history_list = tk.Listbox(history_frame, height=8)
        self.history_list.pack(fill=tk.BOTH, expand=True)
        self.history_list.bind("<Double-Button-1>", self._on_history_double_click)

    def _build_output_ui(self) -> None:
        outer = ttk.Frame(self.output_window, padding=10)
        outer.pack(fill=tk.BOTH, expand=True)

        toolbar = ttk.Frame(outer)
        toolbar.pack(fill=tk.X)
        ttk.Checkbutton(
            toolbar,
            text="Show timestamps",
            variable=self.show_timestamps_var,
        ).pack(side=tk.LEFT)

        self.output_text = scrolledtext.ScrolledText(
            outer,
            wrap=tk.WORD,
            font=("Menlo", 11),
        )
        self.output_text.pack(fill=tk.BOTH, expand=True, pady=(8, 0))

        self.output_text.tag_configure("status", foreground="#1f6feb")
        self.output_text.tag_configure("stderr", foreground="#d1242f")
        self.output_text.tag_configure("ws", foreground="#8250df")

    def _on_send_prompt(self) -> None:
        prompt = self.prompt_text.get("1.0", tk.END).strip()
        if not prompt:
            self._append_status("Prompt is empty.")
            return

        mode = self.mode_var.get().strip() or "exec"
        if mode not in {"exec", "ws", "both"}:
            self._append_status(f"Invalid mode: {mode}")
            return

        self.history_list.insert(tk.END, prompt.splitlines()[0][:140])

        if mode in {"ws", "both"} and not self.ws_mirror.is_running():
            self._on_start_ws()
        if mode in {"ws", "both"}:
            self.ws_writer.send_prompt(self.ws_uri_var.get().strip() or DEFAULT_WS_URI, prompt)

        if mode in {"exec", "both"}:
            command = build_exec_command(
                binary=self.binary_var.get().strip(),
                prompt=prompt,
                profile=self.profile_var.get().strip() or None,
                model=self.model_var.get().strip() or None,
                cwd_override=self.cwd_var.get().strip() or None,
                skip_git_repo_check=self.skip_git_var.get(),
                ephemeral=self.ephemeral_var.get(),
                extra_args=[],
            )

            started = self.runner.start(command, self.cwd_var.get().strip() or str(Path.cwd()))
            if not started:
                self._append_status("A process is already running. Stop it first.")
                return

        self.prompt_text.delete("1.0", tk.END)

    def _on_history_double_click(self, _event: object) -> None:
        selection = self.history_list.curselection()
        if not selection:
            return
        text = self.history_list.get(selection[0])
        self.prompt_text.delete("1.0", tk.END)
        self.prompt_text.insert("1.0", text)

    def _on_start_ws(self) -> None:
        uri = self.ws_uri_var.get().strip() or DEFAULT_WS_URI
        started = self.ws_mirror.start(uri)
        if not started:
            self._append_status("WS mirror already active (or websockets dependency missing).")

    def _on_stop_ws(self) -> None:
        self.ws_mirror.stop()

    def _on_stop_exec(self) -> None:
        self.runner.stop()

    def _clear_output(self) -> None:
        self.output_text.delete("1.0", tk.END)

    def _drain_events(self) -> None:
        while True:
            try:
                event = self.event_queue.get_nowait()
            except queue.Empty:
                break

            ts = event.timestamp.strftime("%H:%M:%S")
            prefix = f"[{ts}] " if self.show_timestamps_var.get() else ""

            if event.source == "status":
                text = f"{prefix}{event.text}"
                tag = "status"
            elif event.source == "stderr":
                text = f"{prefix}{event.text}"
                tag = "stderr"
            elif event.source == "ws":
                text = event.text
                tag = "ws"
            else:
                text = event.text
                tag = None

            if tag:
                self.output_text.insert(tk.END, text, tag)
            else:
                self.output_text.insert(tk.END, text)
            self.output_text.see(tk.END)

        self.root.after(50, self._drain_events)

    def _on_close(self) -> None:
        self.ws_mirror.stop()
        self.runner.stop()
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def run_headless(args: argparse.Namespace) -> int:
    prompts = list(args.prompt)
    if not prompts:
        print("--headless requires at least one --prompt", file=sys.stderr)
        return 2

    event_q: "queue.Queue[StreamEvent]" = queue.Queue()

    def emit(event: StreamEvent) -> None:
        event_q.put(event)

    runner = ExecRunner(emit)

    for prompt in prompts:
        command = build_exec_command(
            binary=detect_codex_binary(args.binary),
            prompt=prompt,
            profile=args.profile,
            model=args.model,
            cwd_override=args.cwd,
            skip_git_repo_check=args.skip_git_repo_check,
            ephemeral=args.ephemeral,
            extra_args=[],
        )
        workdir = args.cwd or str(Path.cwd())
        if not runner.start(command, workdir):
            print("failed to start runner", file=sys.stderr)
            return 1

        while runner.is_running() or not event_q.empty():
            try:
                event = event_q.get(timeout=0.05)
            except queue.Empty:
                continue

            if event.source == "status":
                stamp = event.timestamp.strftime("%H:%M:%S")
                print(f"[{stamp}] {event.text}", end="")
            elif event.source == "stderr":
                print(event.text, end="", file=sys.stderr)
            else:
                print(event.text, end="")

        print("\n--- prompt done ---\n")

    return 0


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Codex Rust GUI client (exec and/or websocket send + stream)",
    )
    parser.add_argument("--binary", default=None, help="Path to codex/codex-rust binary")
    parser.add_argument("--profile", default="HS_OG", help="Config profile (-p)")
    parser.add_argument("--model", default="", help="Model override (-m)")
    parser.add_argument("--cwd", default=None, help="Working directory")
    parser.add_argument("--ws-uri", default=DEFAULT_WS_URI, help="WebSocket URI")
    parser.add_argument(
        "--mode",
        choices=["exec", "ws", "both"],
        default="exec",
        help="Default send mode",
    )
    parser.add_argument(
        "--skip-git-repo-check",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Pass --skip-git-repo-check to codex exec",
    )
    parser.add_argument(
        "--ephemeral",
        action="store_true",
        help="Pass --ephemeral to codex exec",
    )
    parser.add_argument(
        "--reconnect-delay",
        type=float,
        default=DEFAULT_RECONNECT_DELAY,
        help="WS reconnect delay in seconds",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run without GUI and execute prompts from --prompt",
    )
    parser.add_argument(
        "--prompt",
        action="append",
        default=[],
        help="Prompt for headless mode (repeatable)",
    )
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)

    if args.headless:
        return run_headless(args)

    try:
        app = CodexGuiClient(args)
    except Exception as exc:
        if messagebox is not None and tk is not None:
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror("Codex GUI Client", str(exc))
            root.destroy()
        else:
            print(f"Failed to start GUI: {exc}", file=sys.stderr)
        return 1

    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
