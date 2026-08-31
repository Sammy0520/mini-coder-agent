from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from benchmarks.claw_swe_bench.preregister import LANGUAGES, build_manifest
from benchmarks.claw_swe_bench.evaluate_experiment import (
    _jobs,
    _load_official_report,
)
from benchmarks.claw_swe_bench.pull_images import sweagent_image
from benchmarks.claw_swe_bench.run_experiment import _schedule
from benchmarks.claw_swe_bench.support import codex_metrics, mini_session_metrics, read_jsonl


class ClawPreregistrationTests(unittest.TestCase):
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
        rows = [
            {
                "instance_id": f"{language.lower()}-{index}",
                "language": language,
                "repo": f"example/{language.lower()}",
                "source_dataset": "verified-mini" if language == "Python" else "multilingual",
            }
            for language in LANGUAGES
            for index in range(10)
        ]
        first = build_manifest(rows, created_at="fixed")
        second = build_manifest(list(reversed(rows)), created_at="fixed")
        self.assertEqual(first, second)
        self.assertEqual(len(first["instances"]), 16)
        for phase in ("phase1", "phase2"):
            phase_rows = [row for row in first["instances"] if row["phase"] == phase]
            self.assertEqual({row["language"] for row in phase_rows}, set(LANGUAGES))
            self.assertEqual([row["first_agent"] for row in phase_rows].count("mini"), 4)
            self.assertEqual([row["first_agent"] for row in phase_rows].count("codex"), 4)

    def test_both_agent_schedule_is_strictly_paired(self) -> None:
        rows = [
            {
                "instance_id": f"{language.lower()}-{index}",
                "language": language,
                "repo": "example/repo",
                "source_dataset": "multilingual",
            }
            for language in LANGUAGES
            for index in range(10)
        ]
        schedule = _schedule(build_manifest(rows, created_at="fixed"), "phase1", "both")
        self.assertEqual(len(schedule), 16)
        for index in range(0, len(schedule), 2):
            pair = schedule[index : index + 2]
            self.assertEqual(pair[0]["instance_id"], pair[1]["instance_id"])
            self.assertNotEqual(pair[0]["agent"], pair[1]["agent"])

    def test_schedule_can_select_one_pair(self) -> None:
        rows = [
            {
                "instance_id": f"{language.lower()}-{index}",
                "language": language,
                "repo": "example/repo",
                "source_dataset": "multilingual",
            }
            for language in LANGUAGES
            for index in range(10)
        ]
        schedule = _schedule(
            build_manifest(rows, created_at="fixed"), "phase1", "both", pair=3
        )
        self.assertEqual(len(schedule), 2)
        self.assertEqual({row["pair_order"] for row in schedule}, {3})
        self.assertEqual(len({row["instance_id"] for row in schedule}), 1)

    def test_evaluation_jobs_split_pinned_sources_for_each_agent(self) -> None:
        rows = [
            {
                "instance_id": f"{language.lower()}-{index}",
                "language": language,
                "repo": "example/repo",
                "source_dataset": "verified-mini" if language == "Python" else "multilingual",
            }
            for language in LANGUAGES
            for index in range(10)
        ]
        manifest = build_manifest(rows, created_at="fixed")
        jobs = _jobs(manifest, phase="phase1", agent="both", run_prefix="pilot")
        self.assertEqual(len(jobs), 4)
        self.assertEqual({job["agent"] for job in jobs}, {"mini", "codex"})
        self.assertEqual(
            {job["source_dataset"] for job in jobs}, {"multilingual", "verified-mini"}
        )
        self.assertEqual(
            sorted(len(job["instance_ids"]) for job in jobs), [1, 1, 7, 7]
        )


class ClawUsageTests(unittest.TestCase):
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
