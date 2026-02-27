from __future__ import annotations

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
        self.assertFalse(args.show_raw_json)

    def test_overrides(self) -> None:
        args = repl.parse_args(
            [
                "--url",
                "ws://10.0.0.2:4222",
                "--approval-policy",
                "on-request",
                "--no-auto-approve",
                "--show-raw-json",
                "--thread-id",
                "thr_123",
            ]
        )

        self.assertEqual(args.url, "ws://10.0.0.2:4222")
        self.assertEqual(args.approval_policy, "on-request")
        self.assertFalse(args.auto_approve)
        self.assertTrue(args.show_raw_json)
        self.assertEqual(args.thread_id, "thr_123")


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


if __name__ == "__main__":
    unittest.main()
