from __future__ import annotations

import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from mini_coder.config import AgentConfig, WireAPI
from mini_coder.exceptions import ConfigurationError


class ConfigTests(unittest.TestCase):
    def test_loads_stage_f_budget_and_retry_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            "os.environ",
            {
                "CODING_AGENT_MAX_SECONDS": "123",
                "CODING_AGENT_MAX_MODEL_CALLS": "7",
                "CODING_AGENT_MAX_TOOL_CALLS": "9",
                "CODING_AGENT_MAX_TOOL_OUTPUT": "1111",
                "CODING_AGENT_MAX_TOTAL_TOOL_OUTPUT": "2222",
                "CODING_AGENT_MAX_TOTAL_TOKENS": "3333",
                "CODING_AGENT_MAX_RETRIES": "4",
                "CODING_AGENT_RETRY_BASE_SECONDS": "0.25",
                "CODING_AGENT_RETRY_MAX_SECONDS": "3.5",
                "CODING_AGENT_STREAMING": "false",
                "CODING_AGENT_PROMPT_CACHE": "true",
                "CODING_AGENT_PROMPT_CACHE_KEY": "test-workflow-v1",
                "CODING_AGENT_MAX_RESPONSE_TOOL_CALLS": "6",
                "CODING_AGENT_MAX_RESPONSE_WRITE_CALLS": "2",
                "CODING_AGENT_MAX_RESPONSE_WRITE_CHARS": "9000",
            },
            clear=True,
        ):
            config = AgentConfig.from_env(directory)

        self.assertEqual(config.max_seconds, 123)
        self.assertEqual(config.max_model_calls, 7)
        self.assertEqual(config.max_tool_calls, 9)
        self.assertEqual(config.max_tool_output_chars, 1111)
        self.assertEqual(config.max_total_tool_output_chars, 2222)
        self.assertEqual(config.max_total_tokens, 3333)
        self.assertEqual(config.max_model_retries, 4)
        self.assertEqual(config.retry_base_seconds, 0.25)
        self.assertEqual(config.retry_max_seconds, 3.5)
        self.assertFalse(config.model_streaming)
        self.assertTrue(config.prompt_cache_enabled)
        self.assertEqual(config.prompt_cache_key, "test-workflow-v1")
        self.assertEqual(config.max_response_tool_calls, 6)
        self.assertEqual(config.max_response_write_calls, 2)
        self.assertEqual(config.max_response_write_chars, 9000)

    def test_loads_subagent_limits_from_toml(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "agent.toml"
            config_path.write_text(
                "subagents_enabled = true\n"
                "max_parallel_subagents = 2\n"
                "max_subagent_batches = 1\n"
                "max_subagent_steps = 4\n"
                "max_subagent_seconds = 90\n"
                "max_subagent_model_calls = 3\n"
                "max_subagent_tool_calls = 8\n"
                "max_subagent_total_tokens = 12000\n"
                "max_subagent_context_tokens = 4000\n"
                "max_subagent_workspace_files = 700\n"
                "max_subagent_workspace_bytes = 9000000\n",
                encoding="utf-8",
            )
            with patch.dict("os.environ", {}, clear=True):
                config = AgentConfig.from_env(root, config_path=config_path)

            self.assertTrue(config.subagents_enabled)
            self.assertEqual(config.max_parallel_subagents, 2)
            self.assertEqual(config.max_subagent_batches, 1)
            self.assertEqual(config.max_subagent_steps, 4)
            self.assertEqual(config.max_subagent_seconds, 90)
            self.assertEqual(config.max_subagent_model_calls, 3)
            self.assertEqual(config.max_subagent_tool_calls, 8)
            self.assertEqual(config.max_subagent_total_tokens, 12000)
            self.assertEqual(config.max_subagent_context_tokens, 4000)
            self.assertEqual(config.max_subagent_workspace_files, 700)
            self.assertEqual(config.max_subagent_workspace_bytes, 9000000)

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

    def test_parallel_read_tools_can_be_disabled_from_toml_or_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "agent.toml"
            config_path.write_text(
                'model = "test-model"\nparallel_read_tools_enabled = false\n',
                encoding="utf-8",
            )

            with patch.dict("os.environ", {"OPENAI_API_KEY": "test"}, clear=True):
                config = AgentConfig.from_env(root, config_path=config_path)
            self.assertFalse(config.parallel_read_tools_enabled)

            with patch.dict(
                "os.environ",
                {
                    "OPENAI_API_KEY": "test",
                    "CODING_AGENT_PARALLEL_READ_TOOLS": "true",
                },
                clear=True,
            ):
                overridden = AgentConfig.from_env(root, config_path=config_path)
            self.assertTrue(overridden.parallel_read_tools_enabled)

    def test_speculative_finish_is_opt_in_and_has_independent_effort(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "agent.toml"
            config_path.write_text(
                'model = "test-model"\n'
                'speculative_finish_enabled = false\n'
                'speculative_finish_delay_ms = 900\n'
                'speculative_finish_reasoning_effort = "low"\n',
                encoding="utf-8",
            )
            with patch.dict(
                "os.environ",
                {
                    "OPENAI_API_KEY": "test",
                    "CODING_AGENT_SPECULATIVE_FINISH": "true",
                    "CODING_AGENT_SPECULATIVE_FINISH_DELAY_MS": "25",
                    "CODING_AGENT_SPECULATIVE_FINISH_REASONING_EFFORT": "medium",
                },
                clear=True,
            ):
                config = AgentConfig.from_env(root, config_path=config_path)

            self.assertTrue(config.speculative_finish_enabled)
            self.assertEqual(config.speculative_finish_delay_ms, 25)
            self.assertEqual(config.speculative_finish_reasoning_effort, "medium")

    def test_context_v2_is_opt_in_and_loads_watermarks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "agent.toml"
            config_path.write_text(
                'model = "test-model"\n'
                "context_compression_v2_enabled = true\n"
                "context_high_watermark_ratio = 0.88\n"
                "context_target_ratio = 0.66\n"
                "context_hot_tool_batches = 4\n"
                "context_min_checkpoint_batches = 7\n",
                encoding="utf-8",
            )
            with patch.dict("os.environ", {"OPENAI_API_KEY": "test"}, clear=True):
                config = AgentConfig.from_env(root, config_path=config_path)

            self.assertTrue(config.context_compression_v2_enabled)
            self.assertEqual(config.context_high_watermark_ratio, 0.88)
            self.assertEqual(config.context_target_ratio, 0.66)
            self.assertEqual(config.context_hot_tool_batches, 4)
            self.assertEqual(config.context_min_checkpoint_batches, 7)

    def test_context_v2_rejects_inverted_watermarks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ConfigurationError):
                AgentConfig(
                    workspace=Path(directory),
                    api_key="test",
                    base_url=None,
                    model="fake",
                    context_target_ratio=0.90,
                    context_high_watermark_ratio=0.80,
                )

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
