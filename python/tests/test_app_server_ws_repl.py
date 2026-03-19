from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
from typing import Any
import unittest
from unittest import mock
import urllib.error

import python.app_server_ws_repl as repl


class ParseReplCommandTests(unittest.TestCase):
    def test_plain_message(self) -> None:
        command = repl.parse_repl_command("hello")

        self.assertEqual(command.kind, "message")
        self.assertEqual(command.arg, "hello")

    def test_exec_command(self) -> None:
        command = repl.parse_repl_command(":exec ls -la")

        self.assertEqual(command.kind, "exec")
        self.assertEqual(command.arg, "ls -la")

    def test_unknown_command(self) -> None:
        command = repl.parse_repl_command(":nonsense value")

        self.assertEqual(command.kind, "unknown")
        self.assertEqual(command.arg, "nonsense value")

    def test_threads_without_arg(self) -> None:
        command = repl.parse_repl_command(":threads")

        self.assertEqual(command.kind, "threads")
        self.assertIsNone(command.arg)


class ParseArgsTests(unittest.TestCase):
    def test_defaults(self) -> None:
        args = repl.parse_args([])

        self.assertEqual(args.url, "ws://127.0.0.1:4222")
        self.assertEqual(args.approval_policy, "never")
        self.assertTrue(args.auto_approve)
        self.assertTrue(args.local_tool_routing)
        self.assertEqual(args.local_tool_shell_mode, "subprocess")
        self.assertEqual(args.local_tool_shell_init, "auto")
        self.assertFalse(args.show_raw_json)
        self.assertEqual(args.input_ui, "prompt-toolkit")
        self.assertTrue(args.gateway_activity_indicator)
        self.assertIsNone(args.token)
        self.assertIsNone(args.provider_id)
        self.assertIsNone(args.providers_json)
        self.assertTrue(args.provider_preflight)
        self.assertEqual(args.provider_preflight_timeout_sec, 8.0)
        self.assertIsNone(args.thread_config_json)
        self.assertIsNone(args.mcp_servers_json)

    def test_overrides(self) -> None:
        args = repl.parse_args(
            [
                "--url",
                "ws://10.0.0.2:4222",
                "--approval-policy",
                "on-request",
                "--no-auto-approve",
                "--no-local-tool-routing",
                "--show-raw-json",
                "--input-ui",
                "raw-tty",
                "--no-gateway-activity-indicator",
                "--thread-id",
                "thr_123",
                "--local-tool-shell-mode",
                "persistent",
                "--local-tool-shell-init",
                "source ~/.zshrc",
                "--token",
                "token-xyz",
                "--provider-id",
                "academic",
                "--providers-json",
                '[{"providerId":"academic","baseUrl":"https://example/v1","apiKey":"k","wireApi":"chat_completions"}]',
                "--no-provider-preflight",
                "--provider-preflight-timeout-sec",
                "3.5",
                "--thread-config-json",
                '{"features.tools_web_search": true}',
                "--mcp-servers-json",
                '{"moodle": {"url": "http://127.0.0.1:8765/mcp"}}',
            ]
        )

        self.assertEqual(args.url, "ws://10.0.0.2:4222")
        self.assertEqual(args.approval_policy, "on-request")
        self.assertFalse(args.auto_approve)
        self.assertFalse(args.local_tool_routing)
        self.assertEqual(args.input_ui, "raw-tty")
        self.assertFalse(args.gateway_activity_indicator)
        self.assertEqual(args.local_tool_shell_mode, "persistent")
        self.assertEqual(args.local_tool_shell_init, "source ~/.zshrc")
        self.assertTrue(args.show_raw_json)
        self.assertEqual(args.thread_id, "thr_123")
        self.assertEqual(args.token, "token-xyz")
        self.assertEqual(args.provider_id, "academic")
        self.assertIn("providerId", args.providers_json)
        self.assertFalse(args.provider_preflight)
        self.assertEqual(args.provider_preflight_timeout_sec, 3.5)
        self.assertIn("features.tools_web_search", args.thread_config_json)
        self.assertIn("moodle", args.mcp_servers_json)


class WordMoveTests(unittest.TestCase):
    def test_move_word_left(self) -> None:
        buffer = list("alpha beta gamma")
        cursor = len(buffer)

        self.assertEqual(repl._move_word_left(buffer, cursor), 11)
        self.assertEqual(repl._move_word_left(buffer, 11), 6)
        self.assertEqual(repl._move_word_left(buffer, 6), 0)

    def test_move_word_right(self) -> None:
        buffer = list("alpha beta gamma")

        self.assertEqual(repl._move_word_right(buffer, 0), 5)
        self.assertEqual(repl._move_word_right(buffer, 5), 10)
        self.assertEqual(repl._move_word_right(buffer, 10), 16)


class StreamFormatTests(unittest.TestCase):
    def test_bold_marker_gets_line_break(self) -> None:
        text = "**Preparing concise German greeting**Hallo!"
        self.assertEqual(
            repl.format_stream_text(text),
            "**Preparing concise German greeting**\r\nHallo!",
        )

    def test_existing_newline_is_preserved(self) -> None:
        text = "**Preparing concise German greeting**\nHallo!"
        self.assertEqual(repl.format_stream_text(text), text)

    def test_ephemeral_prefers_bold_chunk(self) -> None:
        text = "**Preparing concise German greeting**Hallo!"
        self.assertEqual(
            repl.format_ephemeral_status_text(text),
            "Preparing concise German greeting",
        )

    def test_ephemeral_compacts_plain_text(self) -> None:
        text = "  step   one\nstep two  "
        self.assertEqual(repl.format_ephemeral_status_text(text), "step one step two")


class PasteHandlingTests(unittest.TestCase):
    def test_normalize_pasted_text_flattens_newlines(self) -> None:
        pasted = "Ziel:\r\nFunktion A\nFunktion B\rEnde"
        self.assertEqual(
            repl._normalize_pasted_text(pasted),  # noqa: SLF001
            "Ziel: Funktion A Funktion B Ende",
        )

    def test_read_until_bracketed_paste_end(self) -> None:
        chunks = iter(list("Line1\nLine2\x1b[201~tail"))

        def _read_char(_: int) -> str:
            return next(chunks, "")

        self.assertEqual(
            repl._read_until_bracketed_paste_end(_read_char),  # noqa: SLF001
            "Line1\nLine2",
        )


class InputUiSelectionTests(unittest.TestCase):
    def test_should_use_prompt_toolkit_respects_raw_tty(self) -> None:
        self.assertFalse(repl._should_use_prompt_toolkit("raw-tty"))  # noqa: SLF001

    def test_should_use_prompt_toolkit_forced(self) -> None:
        self.assertTrue(repl._should_use_prompt_toolkit("prompt-toolkit"))  # noqa: SLF001


class GatewayHelpersTests(unittest.TestCase):
    def test_uri_with_token_replaces_query_token(self) -> None:
        uri = repl.uri_with_token("ws://127.0.0.1:4321/ws?foo=1&token=old", "new")
        self.assertIn("token=new", uri)
        self.assertIn("foo=1", uri)
        self.assertNotIn("token=old", uri)

    def test_load_gateway_providers_from_json(self) -> None:
        providers = repl.load_gateway_providers(
            '[{"providerId":"p","baseUrl":"https://x","apiKey":"k","wireApi":"responses"}]'
        )
        assert providers is not None
        self.assertEqual(len(providers), 1)
        self.assertEqual(providers[0]["providerId"], "p")

    def test_load_gateway_providers_from_file_reference(self) -> None:
        payload = [
            {
                "providerId": "p",
                "baseUrl": "https://x",
                "apiKey": "k",
                "wireApi": "responses",
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = f"{tmp}/providers.json"
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            providers = repl.load_gateway_providers(f"@{path}")
        assert providers is not None
        self.assertEqual(providers[0]["wireApi"], "responses")

    def test_find_gateway_provider(self) -> None:
        provider = repl._find_gateway_provider(  # noqa: SLF001
            [{"providerId": "a"}, {"providerId": "b"}],
            "b",
        )
        assert provider is not None
        self.assertEqual(provider["providerId"], "b")

    def test_load_thread_config_overrides(self) -> None:
        overrides = repl.load_thread_config_overrides('{"features.tools_web_search": true}')
        assert overrides is not None
        self.assertTrue(overrides["features.tools_web_search"])

    def test_load_thread_config_overrides_from_file_reference(self) -> None:
        payload = {"features.tools_web_search": True}
        with tempfile.TemporaryDirectory() as tmp:
            path = f"{tmp}/thread_config.json"
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            overrides = repl.load_thread_config_overrides(f"@{path}")
        assert overrides is not None
        self.assertTrue(overrides["features.tools_web_search"])

    def test_load_mcp_server_overrides(self) -> None:
        overrides = repl.load_mcp_server_overrides(
            '{"moodle":{"url":"http://127.0.0.1:8765/mcp"}}'
        )
        assert overrides is not None
        self.assertIn("mcp_servers.moodle", overrides)
        self.assertEqual(overrides["mcp_servers.moodle"]["url"], "http://127.0.0.1:8765/mcp")

    def test_build_thread_config_overrides_merges_sources(self) -> None:
        merged = repl.build_thread_config_overrides(
            '{"features.tools_web_search": true}',
            '{"moodle":{"url":"http://127.0.0.1:8765/mcp"}}',
        )
        assert merged is not None
        self.assertTrue(merged["features.tools_web_search"])
        self.assertIn("mcp_servers.moodle", merged)

    def test_probe_gateway_provider_models_success(self) -> None:
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        with mock.patch("python.app_server_ws_repl.urllib.request.urlopen", return_value=response):
            ok, detail = repl._probe_gateway_provider_models(  # noqa: SLF001
                "https://example.invalid/v1",
                "key",
                1.0,
            )
        self.assertTrue(ok)
        self.assertEqual(detail, "HTTP 200")

    def test_probe_gateway_provider_models_network_error(self) -> None:
        with mock.patch(
            "python.app_server_ws_repl.urllib.request.urlopen",
            side_effect=urllib.error.URLError("timed out"),
        ):
            ok, detail = repl._probe_gateway_provider_models(  # noqa: SLF001
                "https://example.invalid/v1",
                "key",
                1.0,
            )
        self.assertFalse(ok)
        self.assertIn("timed out", detail)


class LocalToolRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_dynamic_tool_call_unknown(self) -> None:
        client = repl.AppServerWsRepl(
            uri="ws://127.0.0.1:4222",
            approval_policy="never",
            model=None,
            cwd=None,
            model_provider=None,
            auto_approve=True,
            final_only=True,
            show_raw_json=False,
            local_tool_routing=True,
            local_tool_shell_mode="subprocess",
            local_tool_shell_init=None,
            gateway_token=None,
            gateway_provider_id=None,
            gateway_providers=None,
            provider_preflight=True,
            provider_preflight_timeout_sec=8.0,
        )

        result = await client._handle_dynamic_tool_call(  # noqa: SLF001
            {"tool": "unknown_tool", "arguments": {}}
        )
        self.assertFalse(result["success"])
        self.assertIn("Unsupported dynamic tool", result["contentItems"][0]["text"])

    async def test_start_thread_merges_thread_config_overrides(self) -> None:
        captured: dict[str, Any] = {}

        async def _request(method: str, params: dict[str, Any] | None) -> dict[str, Any]:
            captured["method"] = method
            captured["params"] = params
            return {"thread": {"id": "thread-1"}}

        client = repl.AppServerWsRepl(
            uri="ws://127.0.0.1:4222",
            approval_policy="never",
            model=None,
            cwd=None,
            model_provider=None,
            auto_approve=True,
            final_only=True,
            show_raw_json=False,
            local_tool_routing=True,
            local_tool_shell_mode="subprocess",
            local_tool_shell_init=None,
            gateway_token=None,
            gateway_provider_id=None,
            gateway_providers=None,
            provider_preflight=False,
            provider_preflight_timeout_sec=8.0,
            thread_config_overrides={
                "mcp_servers.moodle": {"url": "http://127.0.0.1:8765/mcp"},
            },
        )
        client.request = _request  # type: ignore[method-assign]

        await client.start_thread()
        params = captured["params"]
        assert params is not None
        self.assertEqual(captured["method"], "thread/start")
        self.assertEqual(
            params["config"]["mcp_servers.moodle"]["url"],
            "http://127.0.0.1:8765/mcp",
        )
        self.assertFalse(params["config"]["features.shell_tool"])

    async def test_dynamic_tool_specs_include_apply_patch(self) -> None:
        client = repl.AppServerWsRepl(
            uri="ws://127.0.0.1:4222",
            approval_policy="never",
            model=None,
            cwd=None,
            model_provider=None,
            auto_approve=True,
            final_only=True,
            show_raw_json=False,
            local_tool_routing=True,
            local_tool_shell_mode="subprocess",
            local_tool_shell_init=None,
            gateway_token=None,
            gateway_provider_id=None,
            gateway_providers=None,
            provider_preflight=False,
            provider_preflight_timeout_sec=8.0,
        )
        with mock.patch("python.app_server_ws_repl.shutil.which", return_value="/usr/local/bin/apply_patch"):
            tool_names = [tool["name"] for tool in client._local_dynamic_tools()]  # noqa: SLF001
        self.assertIn("apply_patch", tool_names)

    async def test_dynamic_tool_specs_hide_apply_patch_when_unavailable(self) -> None:
        client = repl.AppServerWsRepl(
            uri="ws://127.0.0.1:4222",
            approval_policy="never",
            model=None,
            cwd=None,
            model_provider=None,
            auto_approve=True,
            final_only=True,
            show_raw_json=False,
            local_tool_routing=True,
            local_tool_shell_mode="subprocess",
            local_tool_shell_init=None,
            gateway_token=None,
            gateway_provider_id=None,
            gateway_providers=None,
            provider_preflight=False,
            provider_preflight_timeout_sec=8.0,
        )
        with mock.patch("python.app_server_ws_repl.shutil.which", return_value=None):
            tool_names = [tool["name"] for tool in client._local_dynamic_tools()]  # noqa: SLF001
        self.assertNotIn("apply_patch", tool_names)

    async def test_handle_dynamic_apply_patch_success(self) -> None:
        class _FakeProcess:
            returncode = 0

            async def communicate(self, _input: bytes) -> tuple[bytes, bytes]:
                return b"Patch applied successfully.", b""

        client = repl.AppServerWsRepl(
            uri="ws://127.0.0.1:4222",
            approval_policy="never",
            model=None,
            cwd=None,
            model_provider=None,
            auto_approve=True,
            final_only=True,
            show_raw_json=False,
            local_tool_routing=True,
            local_tool_shell_mode="subprocess",
            local_tool_shell_init=None,
            gateway_token=None,
            gateway_provider_id=None,
            gateway_providers=None,
            provider_preflight=False,
            provider_preflight_timeout_sec=8.0,
        )
        with mock.patch(
            "python.app_server_ws_repl.asyncio.create_subprocess_exec",
            return_value=_FakeProcess(),
        ):
            with mock.patch("python.app_server_ws_repl.shutil.which", return_value="/usr/local/bin/apply_patch"):
                result = await client._handle_dynamic_apply_patch(  # noqa: SLF001
                    {"patch": "*** Begin Patch\n*** End Patch\n"}
                )
        self.assertTrue(result["success"])
        self.assertIn("Patch applied successfully.", result["contentItems"][0]["text"])

    async def test_handle_dynamic_apply_patch_requires_patch(self) -> None:
        client = repl.AppServerWsRepl(
            uri="ws://127.0.0.1:4222",
            approval_policy="never",
            model=None,
            cwd=None,
            model_provider=None,
            auto_approve=True,
            final_only=True,
            show_raw_json=False,
            local_tool_routing=True,
            local_tool_shell_mode="subprocess",
            local_tool_shell_init=None,
            gateway_token=None,
            gateway_provider_id=None,
            gateway_providers=None,
            provider_preflight=False,
            provider_preflight_timeout_sec=8.0,
        )
        result = await client._handle_dynamic_apply_patch({})  # noqa: SLF001
        self.assertFalse(result["success"])
        self.assertIn("requires a 'patch' string", result["contentItems"][0]["text"])

    async def test_handle_dynamic_apply_patch_missing_binary_reports_clear_error(self) -> None:
        client = repl.AppServerWsRepl(
            uri="ws://127.0.0.1:4222",
            approval_policy="never",
            model=None,
            cwd=None,
            model_provider=None,
            auto_approve=True,
            final_only=True,
            show_raw_json=False,
            local_tool_routing=True,
            local_tool_shell_mode="subprocess",
            local_tool_shell_init=None,
            gateway_token=None,
            gateway_provider_id=None,
            gateway_providers=None,
            provider_preflight=False,
            provider_preflight_timeout_sec=8.0,
        )
        with mock.patch("python.app_server_ws_repl.shutil.which", return_value=None):
            result = await client._handle_dynamic_apply_patch(  # noqa: SLF001
                {"patch": "*** Begin Patch\n*** End Patch\n"}
            )
        self.assertFalse(result["success"])
        self.assertIn("not a read-only indicator", result["contentItems"][0]["text"])

    async def test_dynamic_exec_command_falls_back_for_unavailable_workdir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = repl.AppServerWsRepl(
                uri="ws://127.0.0.1:4222",
                approval_policy="never",
                model=None,
                cwd=tmp,
                model_provider=None,
                auto_approve=True,
                final_only=True,
                show_raw_json=False,
                local_tool_routing=True,
                local_tool_shell_mode="subprocess",
                local_tool_shell_init=None,
                gateway_token=None,
                gateway_provider_id=None,
                gateway_providers=None,
                provider_preflight=False,
                provider_preflight_timeout_sec=8.0,
            )
            fake_process = SimpleNamespace(returncode=0, stdin=None, stdout=None, stderr=None)
            fake_session = SimpleNamespace(
                session_id=1,
                process=fake_process,
                completion_event=SimpleNamespace(is_set=lambda: True),
                persistent_shell=False,
                started_at=0.0,
            )
            with mock.patch(
                "python.app_server_ws_repl.asyncio.create_subprocess_exec",
                new=mock.AsyncMock(return_value=fake_process),
            ) as create_proc:
                with mock.patch.object(client, "_register_local_session", return_value=fake_session):
                    with mock.patch.object(client, "_collect_session_output", new=mock.AsyncMock(return_value=("ok", 1))):
                        with mock.patch.object(client, "_finalize_local_session", new=mock.AsyncMock()):
                            with mock.patch.object(client, "_format_dynamic_exec_output", return_value="formatted") as formatter:
                                result = await client._handle_dynamic_exec_command(  # noqa: SLF001
                                    {"cmd": "pwd", "workdir": "/definitely/missing/path"}
                                )

            self.assertTrue(result["success"])
            self.assertEqual(create_proc.await_args.kwargs["cwd"], tmp)
            self.assertIsNotNone(formatter.call_args.kwargs["workdir_note"])

    async def test_dynamic_apply_patch_falls_back_for_unavailable_workdir(self) -> None:
        class _FakeProcess:
            returncode = 0

            async def communicate(self, _input: bytes) -> tuple[bytes, bytes]:
                return b"Patch applied successfully.", b""

        with tempfile.TemporaryDirectory() as tmp:
            client = repl.AppServerWsRepl(
                uri="ws://127.0.0.1:4222",
                approval_policy="never",
                model=None,
                cwd=tmp,
                model_provider=None,
                auto_approve=True,
                final_only=True,
                show_raw_json=False,
                local_tool_routing=True,
                local_tool_shell_mode="subprocess",
                local_tool_shell_init=None,
                gateway_token=None,
                gateway_provider_id=None,
                gateway_providers=None,
                provider_preflight=False,
                provider_preflight_timeout_sec=8.0,
            )
            with mock.patch(
                "python.app_server_ws_repl.asyncio.create_subprocess_exec",
                new=mock.AsyncMock(return_value=_FakeProcess()),
            ) as create_proc:
                with mock.patch("python.app_server_ws_repl.shutil.which", return_value="/usr/local/bin/apply_patch"):
                    result = await client._handle_dynamic_apply_patch(  # noqa: SLF001
                        {"patch": "*** Begin Patch\n*** End Patch\n", "workdir": "/definitely/missing/path"}
                    )

            self.assertTrue(result["success"])
            self.assertEqual(create_proc.await_args.kwargs["cwd"], tmp)
            self.assertIn("Requested workdir", result["contentItems"][0]["text"])


class PersistentShellHelpersTests(unittest.TestCase):
    def _client(self) -> repl.AppServerWsRepl:
        return repl.AppServerWsRepl(
            uri="ws://127.0.0.1:4222",
            approval_policy="never",
            model=None,
            cwd=None,
            model_provider=None,
            auto_approve=True,
            final_only=True,
            show_raw_json=False,
            local_tool_routing=True,
            local_tool_shell_mode="persistent",
            local_tool_shell_init=None,
            gateway_token=None,
            gateway_provider_id=None,
            gateway_providers=None,
            provider_preflight=True,
            provider_preflight_timeout_sec=8.0,
        )

    def test_shell_kind_detection(self) -> None:
        client = self._client()
        self.assertEqual(client._shell_kind_for_path("/bin/zsh"), "posix")  # noqa: SLF001
        self.assertEqual(client._shell_kind_for_path("pwsh"), "powershell")  # noqa: SLF001
        self.assertEqual(client._shell_kind_for_path("cmd.exe"), "cmd")  # noqa: SLF001

    def test_persistent_shell_argv_posix_uses_non_interactive_login(self) -> None:
        client = self._client()
        self.assertEqual(client._persistent_shell_argv("/bin/zsh", True), ["/bin/zsh", "-l"])  # noqa: SLF001
        self.assertEqual(client._persistent_shell_argv("/bin/zsh", False), ["/bin/zsh"])  # noqa: SLF001

    def test_auto_shell_init_command(self) -> None:
        client = self._client()
        self.assertEqual(
            client._auto_shell_init_command("posix", "/bin/zsh"),  # noqa: SLF001
            "[ -f ~/.zshrc ] && source ~/.zshrc",
        )
        self.assertIn(
            ".cshrc",
            client._auto_shell_init_command("csh", "/bin/tcsh") or "",  # noqa: SLF001
        )

    def test_posix_command_with_marker_wraps_cd_and_printf(self) -> None:
        client = self._client()
        command = client._command_with_marker("echo hi", "/tmp/test dir", "M", "posix")  # noqa: SLF001
        self.assertIn("cd '/tmp/test dir'", command)
        self.assertIn("echo hi", command)
        self.assertIn("printf 'M:%s\\n' \"$?\"", command)

    def test_local_tool_process_env_removes_inherited_virtual_env(self) -> None:
        client = self._client()
        venv_dir = "/tmp/inherited-venv"
        with mock.patch.dict(
            os.environ,
            {
                "VIRTUAL_ENV": venv_dir,
                "PYTHONHOME": "x",
                "PYTHONPATH": "y",
                "PATH": f"{venv_dir}/bin:/usr/bin:/bin",
            },
            clear=False,
        ):
            env = client._local_tool_process_env()  # noqa: SLF001

        self.assertNotIn("VIRTUAL_ENV", env)
        self.assertNotIn("PYTHONHOME", env)
        self.assertNotIn("PYTHONPATH", env)
        self.assertEqual(env["PATH"], "/usr/bin:/bin")

    def test_local_tool_process_env_keeps_path_without_virtual_env(self) -> None:
        client = self._client()
        with mock.patch.dict(os.environ, {"PATH": "/usr/local/bin:/usr/bin"}, clear=False):
            env = client._local_tool_process_env()  # noqa: SLF001
        self.assertEqual(env["PATH"], "/usr/local/bin:/usr/bin")

    def test_rewrite_python_command_uses_uv_project_when_pyproject_exists(self) -> None:
        client = self._client()
        with tempfile.TemporaryDirectory() as tmp:
            workdir = tmp
            with open(f"{workdir}/pyproject.toml", "w", encoding="utf-8") as handle:
                handle.write("[project]\nname='x'\nversion='0.1.0'\n")
            rewritten = client._rewrite_python_command_with_uv("python script.py", workdir)  # noqa: SLF001
        self.assertIn("uv run --project", rewritten)
        self.assertTrue(rewritten.endswith("python script.py"))

    def test_rewrite_python_command_with_python_c_semicolon(self) -> None:
        client = self._client()
        with tempfile.TemporaryDirectory() as tmp:
            workdir = tmp
            with open(f"{workdir}/pyproject.toml", "w", encoding="utf-8") as handle:
                handle.write("[project]\nname='x'\nversion='0.1.0'\n")
            rewritten = client._rewrite_python_command_with_uv(  # noqa: SLF001
                'python -c "import sys; print(sys.executable)"',
                workdir,
            )
        self.assertIn("uv run --project", rewritten)

    def test_rewrite_python_command_keeps_explicit_uv_run(self) -> None:
        client = self._client()
        with tempfile.TemporaryDirectory() as tmp:
            rewritten = client._rewrite_python_command_with_uv(  # noqa: SLF001
                "uv run python script.py",
                tmp,
            )
        self.assertEqual(rewritten, "uv run python script.py")

    def test_resolve_local_tool_workdir_falls_back_to_client_cwd(self) -> None:
        client = self._client()
        with tempfile.TemporaryDirectory() as tmp:
            client.cwd = tmp
            resolved, note = client._resolve_local_tool_workdir("/definitely/missing/path")  # noqa: SLF001
        self.assertEqual(resolved, tmp)
        self.assertIsNotNone(note)

    def test_resolve_local_tool_workdir_resolves_existing_relative_path(self) -> None:
        client = self._client()
        with tempfile.TemporaryDirectory() as tmp:
            nested = Path(tmp) / "nested"
            nested.mkdir()
            client.cwd = tmp
            resolved, note = client._resolve_local_tool_workdir("nested")  # noqa: SLF001
        self.assertEqual(resolved, str(nested.resolve()))
        self.assertIsNone(note)


class NotificationHandlingTests(unittest.TestCase):
    def _client(self) -> repl.AppServerWsRepl:
        return repl.AppServerWsRepl(
            uri="ws://127.0.0.1:4222",
            approval_policy="never",
            model=None,
            cwd=None,
            model_provider=None,
            auto_approve=True,
            final_only=True,
            show_raw_json=False,
            local_tool_routing=True,
            local_tool_shell_mode="subprocess",
            local_tool_shell_init=None,
            gateway_token=None,
            gateway_provider_id=None,
            gateway_providers=None,
            provider_preflight=True,
            provider_preflight_timeout_sec=8.0,
        )

    def test_agent_message_notification_prints_message(self) -> None:
        client = self._client()
        with mock.patch.object(client, "_clear_ephemeral_status") as clear_status:
            with mock.patch("builtins.print") as print_mock:
                client._handle_notification(
                    "codex/event/agent_message",
                    {"msg": {"type": "agent_message", "message": "Hello from provider"}},
                )
        clear_status.assert_called_once()
        print_mock.assert_called_once_with("Hello from provider", end="\n", flush=True)


if __name__ == "__main__":
    unittest.main()
