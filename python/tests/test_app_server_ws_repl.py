from __future__ import annotations

import json
import tempfile
import unittest

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
        self.assertFalse(args.show_raw_json)
        self.assertIsNone(args.token)
        self.assertIsNone(args.provider_id)
        self.assertIsNone(args.providers_json)

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
                "--thread-id",
                "thr_123",
                "--local-tool-shell-mode",
                "persistent",
                "--token",
                "token-xyz",
                "--provider-id",
                "academic",
                "--providers-json",
                '[{"providerId":"academic","baseUrl":"https://example/v1","apiKey":"k","wireApi":"chat_completions"}]',
            ]
        )

        self.assertEqual(args.url, "ws://10.0.0.2:4222")
        self.assertEqual(args.approval_policy, "on-request")
        self.assertFalse(args.auto_approve)
        self.assertFalse(args.local_tool_routing)
        self.assertEqual(args.local_tool_shell_mode, "persistent")
        self.assertTrue(args.show_raw_json)
        self.assertEqual(args.thread_id, "thr_123")
        self.assertEqual(args.token, "token-xyz")
        self.assertEqual(args.provider_id, "academic")
        self.assertIn("providerId", args.providers_json)


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
            gateway_token=None,
            gateway_provider_id=None,
            gateway_providers=None,
        )

        result = await client._handle_dynamic_tool_call(  # noqa: SLF001
            {"tool": "unknown_tool", "arguments": {}}
        )
        self.assertFalse(result["success"])
        self.assertIn("Unsupported dynamic tool", result["contentItems"][0]["text"])


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
            gateway_token=None,
            gateway_provider_id=None,
            gateway_providers=None,
        )

    def test_shell_kind_detection(self) -> None:
        client = self._client()
        self.assertEqual(client._shell_kind_for_path("/bin/zsh"), "posix")  # noqa: SLF001
        self.assertEqual(client._shell_kind_for_path("pwsh"), "powershell")  # noqa: SLF001
        self.assertEqual(client._shell_kind_for_path("cmd.exe"), "cmd")  # noqa: SLF001

    def test_posix_command_with_marker_wraps_cd_and_printf(self) -> None:
        client = self._client()
        command = client._command_with_marker("echo hi", "/tmp/test dir", "M", "posix")  # noqa: SLF001
        self.assertIn("cd '/tmp/test dir'", command)
        self.assertIn("echo hi", command)
        self.assertIn("printf 'M:%s\\n' \"$?\"", command)


if __name__ == "__main__":
    unittest.main()
