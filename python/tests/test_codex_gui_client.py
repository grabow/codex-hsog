from __future__ import annotations

import unittest
from unittest import mock
import tempfile
from pathlib import Path

import python.codex_gui_client as client


class BuildExecCommandTests(unittest.TestCase):
    def test_build_exec_command_with_all_options(self) -> None:
        command = client.build_exec_command(
            binary="/tmp/codex",
            prompt="write a rust program",
            profile="HS_OG",
            model="gpt-5",
            cwd_override="/tmp/work",
            skip_git_repo_check=True,
            ephemeral=True,
            extra_args=["--json"],
        )

        self.assertEqual(
            command,
            [
                "/tmp/codex",
                "exec",
                "-p",
                "HS_OG",
                "-m",
                "gpt-5",
                "-C",
                "/tmp/work",
                "--skip-git-repo-check",
                "--ephemeral",
                "--json",
                "write a rust program",
            ],
        )

    def test_build_exec_command_minimal(self) -> None:
        command = client.build_exec_command(
            binary="codex",
            prompt="hello",
            profile=None,
            model=None,
            cwd_override=None,
            skip_git_repo_check=False,
            ephemeral=False,
            extra_args=[],
        )

        self.assertEqual(command, ["codex", "exec", "hello"])


class DetectBinaryTests(unittest.TestCase):
    @mock.patch("python.codex_gui_client.shutil.which")
    def test_detect_codex_binary_prefers_preferred_when_resolvable(self, which_mock: mock.Mock) -> None:
        which_mock.side_effect = lambda value: "/usr/local/bin/codex" if value == "codex" else None

        result = client.detect_codex_binary("codex")

        self.assertEqual(result, "/usr/local/bin/codex")

    @mock.patch("python.codex_gui_client.shutil.which")
    def test_detect_codex_binary_falls_back_to_existing_path(
        self,
        which_mock: mock.Mock,
    ) -> None:
        which_mock.return_value = None

        with tempfile.TemporaryDirectory() as tmp:
            binary_path = Path(tmp) / "codex-custom"
            binary_path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            binary_path.chmod(0o755)
            result = client.detect_codex_binary(str(binary_path))

        self.assertEqual(result, str(binary_path))


class ParseArgsTests(unittest.TestCase):
    def test_parse_args_headless_and_prompts(self) -> None:
        args = client.parse_args(
            [
                "--headless",
                "--prompt",
                "first",
                "--prompt",
                "second",
                "--mode",
                "both",
                "--no-skip-git-repo-check",
            ]
        )

        self.assertTrue(args.headless)
        self.assertEqual(args.prompt, ["first", "second"])
        self.assertEqual(args.mode, "both")
        self.assertFalse(args.skip_git_repo_check)


class WebSocketPayloadTests(unittest.TestCase):
    def test_build_ws_prompt_payload(self) -> None:
        payload = client.build_ws_prompt_payload("hallo")

        self.assertEqual(payload, '{"text": "hallo"}')


if __name__ == "__main__":
    unittest.main()
