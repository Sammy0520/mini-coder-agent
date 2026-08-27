from __future__ import annotations

import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from mini_coder.config import AgentConfig, WireAPI
from mini_coder.exceptions import ConfigurationError


class ConfigTests(unittest.TestCase):
    def test_loads_provider_toml_and_keeps_key_in_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "agent.toml"
            config_path.write_text(
                'model_provider = "aicode007"\n'
                'model = "gpt-5.6-sol"\n'
                'model_reasoning_effort = "xhigh"\n'
                'model_verbosity = "high"\n\n'
                '[model_providers.aicode007]\n'
                'name = "aicode007"\n'
                'base_url = "https://api.aicode007.com"\n'
                'wire_api = "responses"\n'
                'requires_openai_auth = true\n',
                encoding="utf-8",
            )
            with patch.dict("os.environ", {"OPENAI_API_KEY": "test-secret"}, clear=True):
                config = AgentConfig.from_env(root, config_path=config_path)

            self.assertEqual(config.model_provider, "aicode007")
            self.assertEqual(config.model, "gpt-5.6-sol")
            self.assertEqual(config.base_url, "https://api.aicode007.com")
            self.assertEqual(config.wire_api, WireAPI.RESPONSES)
            self.assertEqual(config.model_reasoning_effort, "xhigh")
            self.assertEqual(config.model_verbosity, "high")
            self.assertEqual(config.api_key, "test-secret")

    def test_loads_gitignored_local_auth_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "agent.toml"
            config_path.write_text('model = "test-model"\n', encoding="utf-8")
            (root / "auth.json").write_text(
                json.dumps(
                    {
                        "auth_mode": "apikey",
                        "OPENAI_API_KEY": "local-test-secret",
                    }
                ),
                encoding="utf-8",
            )

            with patch.dict("os.environ", {}, clear=True):
                config = AgentConfig.from_env(root, config_path=config_path)

            self.assertEqual(config.api_key, "local-test-secret")

    def test_environment_key_overrides_local_auth_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "agent.toml"
            config_path.write_text('model = "test-model"\n', encoding="utf-8")
            (root / "auth.json").write_text(
                '{"auth_mode":"apikey","OPENAI_API_KEY":"local-test-secret"}',
                encoding="utf-8",
            )

            with patch.dict(
                "os.environ", {"OPENAI_API_KEY": "environment-test-secret"}, clear=True
            ):
                config = AgentConfig.from_env(root, config_path=config_path)

            self.assertEqual(config.api_key, "environment-test-secret")

    def test_rejects_invalid_local_auth_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "agent.toml"
            config_path.write_text('model = "test-model"\n', encoding="utf-8")
            (root / "auth.json").write_text(
                '{"auth_mode":"apikey"}', encoding="utf-8"
            )

            with patch.dict("os.environ", {}, clear=True):
                with self.assertRaises(ConfigurationError):
                    AgentConfig.from_env(root, config_path=config_path)

    def test_rejects_markdown_link_as_base_url(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ConfigurationError):
                AgentConfig(
                    workspace=Path(directory),
                    api_key="test",
                    base_url="[https://example.test](https://example.test)",
                    model="fake",
                )

    def test_rejects_non_positive_step_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ConfigurationError):
                AgentConfig(
                    workspace=Path(directory),
                    api_key="test",
                    base_url=None,
                    model="fake",
                    max_steps=0,
                )

    def test_live_validation_requires_key_and_model(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = AgentConfig(
                workspace=Path(directory),
                api_key=None,
                base_url=None,
                model=None,
            )
            with self.assertRaises(ConfigurationError):
                config.validate_for_model()


if __name__ == "__main__":
    unittest.main()
