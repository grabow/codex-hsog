from __future__ import annotations

import json
import tempfile
from pathlib import Path
import unittest

import python.app_server_stdio_gateway as gateway


class AuthParseTests(unittest.TestCase):
    def test_prefers_bearer_header(self) -> None:
        token = gateway._parse_bearer_token({"Authorization": "Bearer abc123"}, "/ws")
        self.assertEqual(token, "abc123")

    def test_falls_back_to_query_param(self) -> None:
        token = gateway._parse_bearer_token({}, "/ws?token=query-token")
        self.assertEqual(token, "query-token")


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
                    }
                ]
            }
        }

        providers = gateway._extract_providers_from_initialize(params)
        self.assertIn("academic", providers)
        self.assertEqual(providers["academic"]["wireApi"], "chat_completions")

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


class TlsValidationTests(unittest.TestCase):
    def test_tls_paths_optional(self) -> None:
        self.assertIsNone(gateway._resolve_tls_paths(None, None))

    def test_tls_paths_require_both_files(self) -> None:
        with self.assertRaises(ValueError):
            gateway._resolve_tls_paths("cert.pem", None)
        with self.assertRaises(ValueError):
            gateway._resolve_tls_paths(None, "key.pem")


if __name__ == "__main__":
    unittest.main()
