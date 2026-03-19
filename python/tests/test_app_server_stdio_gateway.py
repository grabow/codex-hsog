from __future__ import annotations

import json as _json
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest

import python.app_server_stdio_gateway as gateway


class AuthParseTests(unittest.TestCase):
    def test_prefers_bearer_header(self) -> None:
        token = gateway._parse_bearer_token({"Authorization": "Bearer abc123"}, "/ws")
        self.assertEqual(token, "abc123")

    def test_falls_back_to_query_param(self) -> None:
        token = gateway._parse_bearer_token({}, "/ws?token=query-token")
        self.assertEqual(token, "query-token")


class ParseArgsTests(unittest.TestCase):
    def test_defaults(self) -> None:
        args = gateway.parse_args([])
        self.assertIsNone(args.users_config)
        self.assertTrue(args.self_register)

    def test_strict_mode_flag(self) -> None:
        args = gateway.parse_args(["--users-config", "users.json", "--no-self-register"])
        self.assertEqual(args.users_config, "users.json")
        self.assertFalse(args.self_register)


class RedactionTests(unittest.TestCase):
    def test_redacts_sensitive_dict_keys(self) -> None:
        value = {
            "apiKey": "secret-123",
            "nested": {
                "Authorization": "Bearer abc",
            },
        }
        redacted = gateway._redact_value(value)
        self.assertEqual(redacted["apiKey"], "<redacted>")
        self.assertEqual(redacted["nested"]["Authorization"], "<redacted>")

    def test_redacts_inline_secret_strings(self) -> None:
        text = "Authorization: Bearer abc123 apiKey=secret"
        redacted = gateway._redact_value(text)
        self.assertNotIn("abc123", redacted)
        self.assertNotIn("secret", redacted)
        self.assertIn("<redacted>", redacted)


class ProviderRewriteTests(unittest.TestCase):
    def test_extract_providers(self) -> None:
        params = {
            "xGateway": {
                "providers": [
                    {
                        "providerId": "academic",
                        "baseUrl": "https://example.edu/v1",
                        "apiKey": "secret",
                        "wireApi": "chat_completions",
                        "fallbackChat": True,
                        "fallbackChatPath": "/chat/completions",
                        "model": "gpt-4.1",
                        "requestMaxRetries": 1,
                        "streamMaxRetries": 0,
                        "streamIdleTimeoutMs": 15000,
                    }
                ]
            }
        }

        providers = gateway._extract_providers_from_initialize(params)
        self.assertIn("academic", providers)
        self.assertEqual(providers["academic"]["wireApi"], "chat_completions")
        self.assertEqual(providers["academic"]["requestMaxRetries"], 1)
        self.assertEqual(providers["academic"]["streamMaxRetries"], 0)
        self.assertEqual(providers["academic"]["streamIdleTimeoutMs"], 15000)

    def test_apply_provider_to_thread_start_sets_model_and_config(self) -> None:
        providers = {
            "academic": {
                "providerId": "academic",
                "baseUrl": "https://example.edu/v1",
                "apiKey": "secret",
                "wireApi": "chat_completions",
                "fallbackChat": True,
                "fallbackChatPath": "/chat/completions",
                "model": "gpt-4.1",
                "requestMaxRetries": 1,
                "streamMaxRetries": 0,
                "streamIdleTimeoutMs": 15000,
            }
        }
        params = {
            "xGateway": {
                "providerId": "academic",
            }
        }

        gateway._apply_provider_to_thread_start(params, providers)

        self.assertEqual(params["modelProvider"], "academic")
        self.assertEqual(params["model"], "gpt-4.1")
        config = params["config"]
        self.assertEqual(config["model_providers.academic.base_url"], "https://example.edu/v1")
        self.assertEqual(config["model_providers.academic.experimental_bearer_token"], "secret")
        self.assertEqual(config["model_providers.academic.wire_api"], "chat_completions")
        self.assertTrue(config["model_providers.academic.fallback_chat"])
        self.assertEqual(config["model_providers.academic.request_max_retries"], 1)
        self.assertEqual(config["model_providers.academic.stream_max_retries"], 0)
        self.assertEqual(config["model_providers.academic.stream_idle_timeout_ms"], 15000)


class ConfigLoadTests(unittest.TestCase):
    def test_load_users_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "users.json"
            config_path.write_text(
                json.dumps(
                    {
                        "users": [
                            {
                                "id": "user-b",
                                "token": "token-b",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            users = gateway.load_user_configs(config_path, tmp_path / "state")
            self.assertEqual(len(users), 1)
            self.assertEqual(users[0].user_id, "user-b")
            self.assertIn("state/user-b/codex_home", str(users[0].codex_home))
            self.assertIn("state/user-b/workspaces", str(users[0].workspace_root))


class ThreadStartCwdResolutionTests(unittest.TestCase):
    def test_uses_client_cwd_when_absolute_existing_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            user = gateway.UserConfig(
                user_id="u1",
                token="tok-1",
                codex_home=tmp_path / "home",
                workspace_root=tmp_path / "workspaces",
            )
            client_dir = tmp_path / "project"
            client_dir.mkdir()
            workspace = tmp_path / "fallback-workspace"

            resolved, source = gateway._resolve_thread_start_cwd(  # noqa: SLF001
                params={"cwd": str(client_dir)},
                user=user,
                new_workspace=lambda: workspace,
            )

            self.assertEqual(resolved, str(client_dir))
            self.assertEqual(source, "client")

    def test_falls_back_for_missing_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            user = gateway.UserConfig(
                user_id="u1",
                token="tok-1",
                codex_home=tmp_path / "home",
                workspace_root=tmp_path / "workspaces",
            )
            workspace = tmp_path / "fallback-workspace"
            workspace.mkdir()

            resolved, source = gateway._resolve_thread_start_cwd(  # noqa: SLF001
                params={},
                user=user,
                new_workspace=lambda: workspace,
            )

            self.assertEqual(resolved, str(workspace))
            self.assertEqual(source, "workspace")

    def test_falls_back_for_relative_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            user = gateway.UserConfig(
                user_id="u1",
                token="tok-1",
                codex_home=tmp_path / "home",
                workspace_root=tmp_path / "workspaces",
            )
            workspace = tmp_path / "fallback-workspace"
            workspace.mkdir()

            with self.assertLogs("app_server_stdio_gateway", level="WARNING") as logs:
                resolved, source = gateway._resolve_thread_start_cwd(  # noqa: SLF001
                    params={"cwd": "relative/path"},
                    user=user,
                    new_workspace=lambda: workspace,
                )

            self.assertEqual(resolved, str(workspace))
            self.assertEqual(source, "workspace")
            self.assertTrue(any("rejected relative cwd" in entry for entry in logs.output))


class McpConfigSanitizationTests(unittest.TestCase):
    def test_required_true_leaf_key_is_downgraded(self) -> None:
        config = {
            "mcp_servers.moodle.url": "http://127.0.0.1:8765/mcp",
            "mcp_servers.moodle.required": True,
        }
        changed = gateway._sanitize_thread_config_for_backend(config)  # noqa: SLF001
        self.assertTrue(changed)
        self.assertFalse(config["mcp_servers.moodle.required"])

    def test_required_true_inside_object_is_downgraded(self) -> None:
        config = {
            "mcp_servers.mail": {
                "url": "http://127.0.0.1:8780/mcp",
                "required": True,
            }
        }
        changed = gateway._sanitize_thread_config_for_backend(config)  # noqa: SLF001
        self.assertTrue(changed)
        self.assertFalse(config["mcp_servers.mail"]["required"])

    def test_required_false_is_left_unchanged(self) -> None:
        config = {
            "mcp_servers.moodle.url": "http://127.0.0.1:8765/mcp",
            "mcp_servers.moodle.required": False,
        }
        changed = gateway._sanitize_thread_config_for_backend(config)  # noqa: SLF001
        self.assertFalse(changed)
        self.assertFalse(config["mcp_servers.moodle.required"])


class SelfRegistrationTests(unittest.TestCase):
    def test_user_id_from_token_is_stable(self) -> None:
        token = "abc-123"
        user_id_1 = gateway.GatewayServer._user_id_from_token(token)  # noqa: SLF001
        user_id_2 = gateway.GatewayServer._user_id_from_token(token)  # noqa: SLF001
        self.assertEqual(user_id_1, user_id_2)
        self.assertTrue(user_id_1.startswith("user_"))

    def test_resolve_user_self_register_creates_user(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server = gateway.GatewayServer(
                listen_host="127.0.0.1",
                listen_port=4321,
                codex_bin=Path("/tmp/codex"),
                users=[],
                workspace_root=Path(tmp),
                allow_self_register=True,
                idle_timeout_seconds=300,
                ssl_context=None,
            )
            user = server._resolve_user("tok-1")  # noqa: SLF001
            self.assertIsNotNone(user)
            assert user is not None
            self.assertEqual(server.users_by_token["tok-1"].user_id, user.user_id)
            self.assertIn(user.user_id, server.workers_by_user)

    def test_duplicate_preconfigured_tokens_emit_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            with self.assertLogs("app_server_stdio_gateway", level="WARNING") as logs:
                gateway.GatewayServer(
                    listen_host="127.0.0.1",
                    listen_port=4321,
                    codex_bin=Path("/tmp/codex"),
                    users=[
                        gateway.UserConfig(
                            user_id="u1",
                            token="same-token",
                            codex_home=tmp_path / "u1" / "home",
                            workspace_root=tmp_path / "u1" / "ws",
                        ),
                        gateway.UserConfig(
                            user_id="u2",
                            token="same-token",
                            codex_home=tmp_path / "u2" / "home",
                            workspace_root=tmp_path / "u2" / "ws",
                        ),
                    ],
                    workspace_root=tmp_path,
                    allow_self_register=False,
                    idle_timeout_seconds=300,
                    ssl_context=None,
                )
        self.assertTrue(any("!!! duplicate token configured" in entry for entry in logs.output))

    def test_resolve_user_warns_when_token_reused_with_active_clients(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server = gateway.GatewayServer(
                listen_host="127.0.0.1",
                listen_port=4321,
                codex_bin=Path("/tmp/codex"),
                users=[],
                workspace_root=Path(tmp),
                allow_self_register=True,
                idle_timeout_seconds=300,
                ssl_context=None,
            )
            first = server._resolve_user("tok-1")  # noqa: SLF001
            assert first is not None
            worker = server.workers_by_user[first.user_id]
            worker.clients.add(object())  # simulate an already connected client
            with self.assertLogs("app_server_stdio_gateway", level="WARNING") as logs:
                second = server._resolve_user("tok-1")  # noqa: SLF001
            self.assertEqual(first.user_id, second.user_id if second else None)
            self.assertTrue(any("!!! token reused across active connections" in entry for entry in logs.output))


class TlsValidationTests(unittest.TestCase):
    def test_tls_paths_optional(self) -> None:
        self.assertIsNone(gateway._resolve_tls_paths(None, None))

    def test_tls_paths_require_both_files(self) -> None:
        with self.assertRaises(ValueError):
            gateway._resolve_tls_paths("cert.pem", None)
        with self.assertRaises(ValueError):
            gateway._resolve_tls_paths(None, "key.pem")


class _FakeWorker:
    def __init__(self) -> None:
        self.forwarded_requests: list[dict] = []
        self.forwarded_backend_messages: list[dict] = []
        self.sent_client_messages: list[dict] = []
        self.added_clients: list[gateway.ClientSession] = []
        self.removed_clients: list[gateway.ClientSession] = []
        self.clients: set[gateway.ClientSession] = set()

    async def add_client(self, client: gateway.ClientSession) -> None:
        self.clients.add(client)
        self.added_clients.append(client)

    async def remove_client(self, client: gateway.ClientSession) -> None:
        self.clients.discard(client)
        self.removed_clients.append(client)

    async def forward_request(self, client: gateway.ClientSession, request: dict) -> None:
        self.forwarded_requests.append(request)

    async def _send_client(self, client: gateway.ClientSession, message: dict) -> None:
        self.sent_client_messages.append(message)

    async def _send_backend_message(self, message: dict) -> None:
        self.forwarded_backend_messages.append(message)

    def touch(self) -> None:
        return


class _FakeWebSocket:
    def __init__(self, messages: list[str], path: str) -> None:
        self._messages = messages
        self._index = 0
        self.request = SimpleNamespace(path=path, headers={})
        self.sent: list[dict] = []
        self.closed = False

    def __aiter__(self) -> _FakeWebSocket:
        return self

    async def __anext__(self) -> str:
        if self._index >= len(self._messages):
            raise StopAsyncIteration
        item = self._messages[self._index]
        self._index += 1
        return item

    async def send(self, data: str) -> None:
        self.sent.append(_json.loads(data))

    async def close(self, code: int | None = None, reason: str | None = None) -> None:
        self.closed = True


class _CaptureWebSocket:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def send(self, data: str) -> None:
        self.messages.append(_json.loads(data))


class UserWorkerInitializeTests(unittest.IsolatedAsyncioTestCase):
    async def test_initialize_includes_gateway_warning_for_shared_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            user = gateway.UserConfig(
                user_id="u1",
                token="tok-1",
                codex_home=tmp_path / "codex_home",
                workspace_root=tmp_path / "workspaces",
            )
            worker = gateway.UserWorker(user=user, codex_bin=Path("/tmp/codex"), idle_timeout_seconds=300)
            worker.backend_initialized = True
            worker.backend_user_agent = "gateway-test-agent"

            ws_primary = _CaptureWebSocket()
            ws_other = _CaptureWebSocket()
            client_primary = gateway.ClientSession(websocket=ws_primary, user=user)
            client_other = gateway.ClientSession(websocket=ws_other, user=user)
            worker.clients.add(client_primary)
            worker.clients.add(client_other)

            await worker.forward_request(
                client_primary,
                {
                    "id": "1",
                    "method": "initialize",
                    "params": {"clientInfo": {"name": "test", "version": "0.1.0"}},
                },
            )

            self.assertEqual(len(ws_primary.messages), 1)
            result = ws_primary.messages[0].get("result", {})
            self.assertEqual(result.get("userAgent"), "gateway-test-agent")
            warning = result.get("gatewayWarning")
            self.assertIsInstance(warning, str)
            self.assertIn("!!! token is currently used by multiple live clients", warning)

    async def test_thread_start_prefers_client_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            user = gateway.UserConfig(
                user_id="u1",
                token="tok-1",
                codex_home=tmp_path / "codex_home",
                workspace_root=tmp_path / "workspaces",
            )
            worker = gateway.UserWorker(user=user, codex_bin=Path("/tmp/codex"), idle_timeout_seconds=300)
            worker.backend_initialized = True

            ws = _CaptureWebSocket()
            client = gateway.ClientSession(websocket=ws, user=user, initialize_done=True, initialized_notified=True)
            worker.clients.add(client)

            backend_messages: list[dict] = []

            async def _capture_backend_message(message: dict) -> None:
                backend_messages.append(message)

            worker._send_backend_message = _capture_backend_message  # type: ignore[method-assign]

            client_dir = tmp_path / "project"
            client_dir.mkdir()
            await worker.forward_request(
                client,
                {
                    "id": "1",
                    "method": "thread/start",
                    "params": {"cwd": str(client_dir)},
                },
            )

            self.assertEqual(len(backend_messages), 1)
            self.assertEqual(backend_messages[0]["method"], "thread/start")
            self.assertEqual(backend_messages[0]["params"]["cwd"], str(client_dir))
            self.assertFalse(user.workspace_root.exists())

    async def test_thread_start_falls_back_to_gateway_workspace_without_client_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            user = gateway.UserConfig(
                user_id="u1",
                token="tok-1",
                codex_home=tmp_path / "codex_home",
                workspace_root=tmp_path / "workspaces",
            )
            worker = gateway.UserWorker(user=user, codex_bin=Path("/tmp/codex"), idle_timeout_seconds=300)
            worker.backend_initialized = True

            ws = _CaptureWebSocket()
            client = gateway.ClientSession(websocket=ws, user=user, initialize_done=True, initialized_notified=True)
            worker.clients.add(client)

            backend_messages: list[dict] = []

            async def _capture_backend_message(message: dict) -> None:
                backend_messages.append(message)

            worker._send_backend_message = _capture_backend_message  # type: ignore[method-assign]

            await worker.forward_request(
                client,
                {
                    "id": "1",
                    "method": "thread/start",
                    "params": {},
                },
            )

            self.assertEqual(len(backend_messages), 1)
            self.assertEqual(backend_messages[0]["method"], "thread/start")
            cwd = backend_messages[0]["params"]["cwd"]
            self.assertTrue(cwd.startswith(str(user.workspace_root)))
            self.assertTrue(Path(cwd).is_dir())

    async def test_thread_start_sanitizes_mcp_required_true(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            user = gateway.UserConfig(
                user_id="u1",
                token="tok-1",
                codex_home=tmp_path / "codex_home",
                workspace_root=tmp_path / "workspaces",
            )
            worker = gateway.UserWorker(user=user, codex_bin=Path("/tmp/codex"), idle_timeout_seconds=300)
            worker.backend_initialized = True

            ws = _CaptureWebSocket()
            client = gateway.ClientSession(websocket=ws, user=user, initialize_done=True, initialized_notified=True)
            worker.clients.add(client)

            backend_messages: list[dict] = []

            async def _capture_backend_message(message: dict) -> None:
                backend_messages.append(message)

            worker._send_backend_message = _capture_backend_message  # type: ignore[method-assign]

            with self.assertLogs("app_server_stdio_gateway", level="WARNING") as logs:
                await worker.forward_request(
                    client,
                    {
                        "id": "1",
                        "method": "thread/start",
                        "params": {
                            "cwd": str(tmp_path),
                            "config": {
                                "mcp_servers.moodle.url": "http://127.0.0.1:8765/mcp",
                                "mcp_servers.moodle.required": True,
                            },
                        },
                    },
                )

            self.assertEqual(len(backend_messages), 1)
            forwarded_config = backend_messages[0]["params"]["config"]
            self.assertFalse(forwarded_config["mcp_servers.moodle.required"])
            self.assertTrue(any("thread config adjusted for backend stability" in entry for entry in logs.output))


class GatewayForwardingTests(unittest.IsolatedAsyncioTestCase):
    async def test_forwards_client_jsonrpc_response_to_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            user = gateway.UserConfig(
                user_id="u1",
                token="tok-1",
                codex_home=tmp_path / "codex_home",
                workspace_root=tmp_path / "workspaces",
            )
            server = gateway.GatewayServer(
                listen_host="127.0.0.1",
                listen_port=4321,
                codex_bin=Path("/tmp/codex"),
                users=[user],
                workspace_root=tmp_path,
                allow_self_register=False,
                idle_timeout_seconds=300,
                ssl_context=None,
            )

            fake_worker = _FakeWorker()
            server.workers_by_user[user.user_id] = fake_worker  # type: ignore[assignment]

            response_message = {
                "id": "tool-call-1",
                "result": {"contentItems": [{"type": "output_text", "text": "ok"}]},
            }
            websocket = _FakeWebSocket(
                messages=[json.dumps(response_message)],
                path="/ws?token=tok-1",
            )

            await server._handle_client(websocket)  # noqa: SLF001

            self.assertEqual(fake_worker.forwarded_backend_messages, [response_message])


if __name__ == "__main__":
    unittest.main()
