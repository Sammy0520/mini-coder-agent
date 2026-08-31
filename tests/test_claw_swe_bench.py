from __future__ import annotations

import json
import io
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from benchmarks.claw_swe_bench.preregister import EASY_DIFFICULTY, build_manifest
from benchmarks.claw_swe_bench.evaluate_experiment import (
    _jobs,
    _load_official_report,
    _write_filtered_predictions,
)
from benchmarks.claw_swe_bench.pull_images import sweagent_image
from benchmarks.claw_swe_bench.network_guard import provider_host
from benchmarks.claw_swe_bench.run_experiment import (
    _purge_infrastructure_attempt,
    _schedule,
)
from benchmarks.claw_swe_bench.support import (
    benchmark_integrity_violations,
    classify_infrastructure_failure,
    classify_process_infrastructure_failure,
    codex_metrics,
    mini_session_metrics,
    ProviderPreflightError,
    provider_preflight,
    read_jsonl,
)


class ClawPreregistrationTests(unittest.TestCase):
    @staticmethod
    def _easy_rows() -> list[dict[str, str]]:
        return [
            {
                "instance_id": f"repo{repo}__task-{index}",
                "language": "Python",
                "repo": f"example/repo{repo}",
                "difficulty": EASY_DIFFICULTY,
                "source_dataset": "verified-easy",
            }
            for repo in range(10)
            for index in range(3)
        ]

    def test_formal_network_guard_requires_https_provider(self) -> None:
        self.assertEqual(provider_host("https://api.aicode007.com"), "api.aicode007.com")
        with self.assertRaisesRegex(ValueError, "https URL"):
            provider_host("http://api.example.test")

    def test_official_report_is_bound_to_prediction_model_and_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            predictions = root / "predictions.jsonl"
            predictions.write_text(
                json.dumps(
                    {
                        "instance_id": "example__repo-1",
                        "model_name_or_path": "vendor/model",
                        "model_patch": "patch",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            report = root / "vendor__model.pilot-eval.json"
            report.write_text(
                json.dumps(
                    {
                        "resolved_ids": ["example__repo-1"],
                        "incomplete_ids": [],
                        "error_ids": [],
                    }
                ),
                encoding="utf-8",
            )
            loaded = _load_official_report(root, "pilot-eval", predictions)
            self.assertEqual(loaded["resolved_ids"], ["example__repo-1"])

    def test_official_sweagent_image_name(self) -> None:
        self.assertEqual(
            sweagent_image("django__django-11885"),
            "swebench/sweb.eval.x86_64.django_1776_django-11885:latest",
        )

    def test_selection_is_balanced_deterministic_and_two_phase(self) -> None:
        rows = self._easy_rows()
        first = build_manifest(rows, created_at="fixed")
        second = build_manifest(list(reversed(rows)), created_at="fixed")
        self.assertEqual(first, second)
        self.assertEqual(len(first["instances"]), 16)
        for phase in ("phase1", "phase2"):
            phase_rows = [row for row in first["instances"] if row["phase"] == phase]
            self.assertEqual(len({row["repo"] for row in phase_rows}), 8)
            self.assertEqual(
                {row["difficulty"] for row in phase_rows}, {EASY_DIFFICULTY}
            )
            self.assertEqual([row["first_agent"] for row in phase_rows].count("mini"), 4)
            self.assertEqual([row["first_agent"] for row in phase_rows].count("codex"), 4)
        self.assertEqual(len({row["instance_id"] for row in first["instances"]}), 16)

    def test_both_agent_schedule_is_strictly_paired(self) -> None:
        rows = self._easy_rows()
        schedule = _schedule(build_manifest(rows, created_at="fixed"), "phase1", "both")
        self.assertEqual(len(schedule), 16)
        for index in range(0, len(schedule), 2):
            pair = schedule[index : index + 2]
            self.assertEqual(pair[0]["instance_id"], pair[1]["instance_id"])
            self.assertNotEqual(pair[0]["agent"], pair[1]["agent"])

    def test_schedule_can_select_one_pair(self) -> None:
        rows = self._easy_rows()
        schedule = _schedule(
            build_manifest(rows, created_at="fixed"), "phase1", "both", pair=3
        )
        self.assertEqual(len(schedule), 2)
        self.assertEqual({row["pair_order"] for row in schedule}, {3})
        self.assertEqual(len({row["instance_id"] for row in schedule}), 1)

    def test_evaluation_jobs_split_pinned_sources_for_each_agent(self) -> None:
        rows = self._easy_rows()
        manifest = build_manifest(rows, created_at="fixed")
        jobs = _jobs(manifest, phase="phase1", agent="both", run_prefix="pilot")
        self.assertEqual(len(jobs), 2)
        self.assertEqual({job["agent"] for job in jobs}, {"mini", "codex"})
        self.assertEqual(
            {job["source_dataset"] for job in jobs}, {"verified-easy"}
        )
        self.assertEqual([len(job["instance_ids"]) for job in jobs], [8, 8])

    def test_predictions_are_filtered_to_the_exact_official_job(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "all.jsonl"
            source.write_text(
                "".join(
                    json.dumps(
                        {
                            "instance_id": instance_id,
                            "model_name_or_path": "agent",
                            "model_patch": patch,
                        }
                    )
                    + "\n"
                    for instance_id, patch in (
                        ("python__one-1", "python patch"),
                        ("rust__two-2", ""),
                    )
                ),
                encoding="utf-8",
            )
            destination = root / "python.jsonl"
            _write_filtered_predictions(source, destination, ["python__one-1"])
            rows = [json.loads(line) for line in destination.read_text().splitlines()]
            self.assertEqual([row["instance_id"] for row in rows], ["python__one-1"])

    def test_prediction_filter_rejects_missing_selected_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "all.jsonl"
            source.write_text(
                '{"instance_id":"python__one-1","model_name_or_path":"agent","model_patch":""}\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "missing selected IDs"):
                _write_filtered_predictions(
                    source, root / "missing.jsonl", ["rust__two-2"]
                )

    def test_infrastructure_attempt_can_be_purged_without_losing_other_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory) / "pilot-mini"
            failed = run / "repo__one-1"
            failed.mkdir(parents=True)
            metadata = failed / "metadata.json"
            metadata.write_text('{"outcome_class":"infrastructure_error"}')
            for name in ("state.jsonl", "predictions.jsonl"):
                (run / name).write_text(
                    '{"instance_id":"repo__one-1"}\n'
                    '{"instance_id":"repo__two-2"}\n',
                    encoding="utf-8",
                )
            _purge_infrastructure_attempt(metadata, "repo__one-1")
            self.assertFalse(failed.exists())
            for name in ("state.jsonl", "predictions.jsonl"):
                rows = [
                    json.loads(line)
                    for line in (run / name).read_text(encoding="utf-8").splitlines()
                ]
                self.assertEqual(
                    [row["instance_id"] for row in rows], ["repo__two-2"]
                )


class ClawUsageTests(unittest.TestCase):
    def test_provider_preflight_redacts_key_and_classifies_billing_error(self) -> None:
        error = urllib.error.HTTPError(
            "https://api.example.test/responses",
            403,
            "Forbidden",
            {},
            io.BytesIO(b'{"error":{"type":"billing_error","message":"insufficient balance"}}'),
        )
        with patch(
            "benchmarks.claw_swe_bench.support.urllib.request.urlopen",
            side_effect=error,
        ) as urlopen:
            with self.assertRaises(ProviderPreflightError) as caught:
                provider_preflight(
                    api_key="secret-test-key",
                    base_url="https://api.example.test",
                    model="model",
                )
        request = urlopen.call_args.args[0]
        self.assertEqual(
            request.get_header("User-agent"),
            "openai-python/2.x mini-coder-benchmark/1.0",
        )
        self.assertEqual(caught.exception.category, "billing")
        self.assertNotIn("secret-test-key", str(caught.exception))

    def test_billing_and_auth_failures_are_infrastructure_errors(self) -> None:
        self.assertEqual(
            classify_infrastructure_failure(
                "Model request failed: billing_error insufficient balance"
            ),
            "billing",
        )
        self.assertEqual(
            classify_infrastructure_failure("HTTP 401 invalid_api_key"),
            "authentication",
        )
        self.assertIsNone(
            classify_infrastructure_failure("tests failed: assertion mismatch")
        )

    def test_successful_agent_prose_does_not_become_infrastructure_error(self) -> None:
        prose = "Preserve the existing 401 Unauthorized authentication behavior."
        self.assertIsNone(
            classify_process_infrastructure_failure(
                stdout=prose, stderr="", exit_code=0
            )
        )
        self.assertEqual(
            classify_process_infrastructure_failure(
                stdout="HTTP 401 invalid_api_key", stderr="", exit_code=1
            ),
            "authentication",
        )

    def test_external_source_lookup_is_an_integrity_violation(self) -> None:
        events = [
            {
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "command": "curl https://api.github.com/search/issues?q=answer",
                },
            },
            {
                "event": "tool_call_requested",
                "tool": "run_command",
                "arguments": {"command": "wget https://example.com/gold.patch"},
            },
            {
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "command": "git log --oneline -5",
                },
            },
        ]
        violations = benchmark_integrity_violations(events)
        self.assertEqual(len(violations), 2)
        self.assertIn("api.github.com", violations[0])

    def test_codex_jsonl_usage_is_normalized(self) -> None:
        events = [
            {"type": "thread.started", "thread_id": "thread-1"},
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 100,
                    "cached_input_tokens": 80,
                    "output_tokens": 20,
                    "reasoning_output_tokens": 7,
                },
            },
            {"type": "item.completed", "item": {"id": "tool-1", "type": "file_change"}},
        ]
        metrics = codex_metrics(events)
        self.assertEqual(metrics["thread_id"], "thread-1")
        self.assertEqual(metrics["cached_tokens"], 80)
        self.assertEqual(metrics["reasoning_tokens"], 7)
        self.assertEqual(metrics["tool_calls"], 1)

    def test_mini_session_usage_and_lenient_jsonl_reader(self) -> None:
        metrics = mini_session_metrics(
            {
                "session_id": "session-1",
                "total_usage": {"input_tokens": 20, "cached_tokens": 10},
                "turn_count": 2,
                "model_call_count": 3,
                "tool_executions": [{}, {}],
            }
        )
        self.assertEqual(metrics["model_calls"], 3)
        self.assertEqual(metrics["tool_calls"], 2)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            path.write_text('{"type":"ok"}\nnot json\n', encoding="utf-8")
            self.assertEqual(read_jsonl(path), [{"type": "ok"}])


if __name__ == "__main__":
    unittest.main()
