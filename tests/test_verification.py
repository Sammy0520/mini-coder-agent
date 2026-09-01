from __future__ import annotations

import unittest

from mini_coder.verification import (
    VerificationRecord,
    VerificationStatus,
    VerificationTracker,
)


class VerificationTrackerTests(unittest.TestCase):
    def test_explicit_purpose_and_common_test_commands_are_classified(self) -> None:
        tracker = VerificationTracker()

        self.assertTrue(
            tracker.is_verification_command(
                {"command": "custom-check --all", "purpose": "verify"}
            )
        )
        self.assertTrue(
            tracker.is_verification_command({"command": "python -m unittest discover"})
        )
        self.assertTrue(tracker.is_verification_command({"command": "npm run build"}))
        self.assertFalse(tracker.is_verification_command({"command": "python --version"}))
        self.assertFalse(
            tracker.is_verification_command(
                {"command": "python -m unittest", "purpose": "inspect"}
            )
        )

    def test_cross_language_checks_are_recognized_as_verification(self) -> None:
        tracker = VerificationTracker()
        commands = (
            "node --check app.js",
            "ruff check src",
            "npm run test:unit",
            "deno check app.ts",
            "cargo clippy",
            "go vet ./...",
            "dotnet test",
            "mvn -q verify",
            "gradlew check",
            "ctest --test-dir build",
            "bundle exec rspec",
            "phpunit",
            "swift test",
            "flutter analyze",
            "mix test",
            "zig test src/main.zig",
            "shellcheck script.sh",
            "terraform validate",
            "helm lint chart",
        )

        for command in commands:
            with self.subTest(command=command):
                self.assertTrue(
                    tracker.is_verification_command({"command": command})
                )

    def test_latest_current_record_controls_status(self) -> None:
        passed = VerificationRecord.create(
            tool_execution_id="execution-pass",
            command="python -m unittest",
            cwd=".",
            exit_code=0,
            duration_seconds=0.1,
            stdout_summary="",
            stderr_summary="OK",
            change_revision=1,
            passed=True,
            timed_out=False,
        )
        failed = VerificationRecord.create(
            tool_execution_id="execution-fail",
            command="python -m unittest",
            cwd=".",
            exit_code=1,
            duration_seconds=0.1,
            stdout_summary="",
            stderr_summary="FAILED",
            change_revision=1,
            passed=False,
            timed_out=False,
        )

        self.assertEqual(
            VerificationTracker.evaluate(
                [passed, failed],
                change_revision=1,
                had_file_modification=True,
            ),
            VerificationStatus.FAILED,
        )
        VerificationTracker.invalidate([passed, failed], reason="file changed")
        self.assertEqual(
            VerificationTracker.evaluate(
                [passed, failed],
                change_revision=2,
                had_file_modification=True,
            ),
            VerificationStatus.STALE,
        )

    def test_expected_nonzero_exit_is_supporting_not_conclusive_verification(self) -> None:
        record = VerificationTracker.record(
            tool_execution_id="negative-check",
            arguments={
                "command": "python -m app invalid-input",
                "purpose": "verify",
                "expected_exit_codes": [2],
                "verification_mode": "expected_rejection",
            },
            result_data={
                "exit_code": 2,
                "duration_seconds": 0.2,
                "stderr": "invalid input",
                "expected_exit_codes": [2],
                "expectation_met": True,
                "timed_out": False,
                "verification_mode": "expected_rejection",
            },
            result_ok=True,
            change_revision=3,
        )

        self.assertTrue(record.passed)
        self.assertTrue(record.expectation_met)
        self.assertEqual(record.expected_exit_codes, (2,))
        self.assertFalse(record.conclusive)
        self.assertEqual(
            VerificationTracker.evaluate(
                [record],
                change_revision=3,
                had_file_modification=True,
            ),
            VerificationStatus.UNVERIFIED,
        )

    def test_documentation_change_keeps_unrelated_test_current(self) -> None:
        record = VerificationRecord.create(
            tool_execution_id="tests",
            command="python -m unittest",
            cwd=".",
            exit_code=0,
            duration_seconds=0.1,
            stdout_summary="OK",
            stderr_summary="",
            change_revision=1,
            passed=True,
            timed_out=False,
        )

        invalidated = VerificationTracker.invalidate(
            [record],
            reason="file changed: README.md",
            changed_path="README.md",
        )

        self.assertEqual(invalidated, [])
        self.assertTrue(record.is_current)
        self.assertEqual(
            VerificationTracker.evaluate(
                [record],
                change_revision=2,
                had_file_modification=True,
            ),
            VerificationStatus.PASSED,
        )

        invalidated = VerificationTracker.invalidate(
            [record],
            reason="file changed: src/app.py",
            changed_path="src/app.py",
        )
        self.assertEqual(invalidated, [record])

    def test_verification_domains_keep_unrelated_frontend_change_current(self) -> None:
        record = VerificationTracker.record(
            tool_execution_id="python-tests",
            arguments={"command": "python -m unittest", "purpose": "verify"},
            result_data={"exit_code": 0, "duration_seconds": 0.1},
            result_ok=True,
            change_revision=1,
        )

        self.assertEqual(record.scope_domains, ("python", "config"))
        self.assertEqual(
            VerificationTracker.invalidate(
                [record], reason="file changed: web/app.css", changed_path="web/app.css"
            ),
            [],
        )
        self.assertEqual(
            VerificationTracker.invalidate(
                [record], reason="file changed: src/app.py", changed_path="src/app.py"
            ),
            [record],
        )

    def test_explicit_verification_paths_limit_invalidation_scope(self) -> None:
        record = VerificationTracker.record(
            tool_execution_id="focused",
            arguments={
                "command": "custom-check",
                "purpose": "verify",
                "verification_paths": ["src/core"],
            },
            result_data={"exit_code": 0, "duration_seconds": 0.1},
            result_ok=True,
            change_revision=1,
        )

        self.assertEqual(
            VerificationTracker.invalidate(
                [record], reason="file changed: docs/a.md", changed_path="docs/a.md"
            ),
            [],
        )
        self.assertEqual(
            VerificationTracker.invalidate(
                [record], reason="file changed: src/core/a.py", changed_path="src/core/a.py"
            ),
            [record],
        )

    def test_corrected_scoped_verifier_supersedes_inline_harness_syntax_error(self) -> None:
        arguments = {
            "command": 'python -c "def broken():\\n pass"',
            "purpose": "verify",
            "verification_paths": ["backend/server.py", "frontend/app.js"],
            "verification_domains": ["python", "web"],
        }
        failed = VerificationTracker.record(
            tool_execution_id="broken-harness",
            arguments=arguments,
            result_data={
                "exit_code": 1,
                "duration_seconds": 0.1,
                "stderr": '  File "<string>", line 1\nSyntaxError: invalid syntax',
            },
            result_ok=False,
            change_revision=4,
        )
        passed = VerificationTracker.record(
            tool_execution_id="corrected-harness",
            arguments={**arguments, "command": 'python -c "print(\'integration ok\')"'},
            result_data={"exit_code": 0, "duration_seconds": 0.1},
            result_ok=True,
            change_revision=4,
        )

        self.assertTrue(failed.environment_error)
        self.assertEqual(
            VerificationTracker.evaluate(
                [failed, passed], change_revision=4, had_file_modification=True
            ),
            VerificationStatus.PASSED,
        )

    def test_real_failure_is_not_hidden_by_different_successful_check(self) -> None:
        common = {
            "purpose": "verify",
            "verification_paths": ["src/app.py"],
            "verification_domains": ["python"],
        }
        failed = VerificationTracker.record(
            tool_execution_id="real-failure",
            arguments={**common, "command": "python -m unittest"},
            result_data={
                "exit_code": 1,
                "duration_seconds": 0.1,
                "stderr": "FAILED (failures=1)",
            },
            result_ok=False,
            change_revision=2,
        )
        passed = VerificationTracker.record(
            tool_execution_id="smoke-pass",
            arguments={**common, "command": 'python -c "print(\'ok\')"'},
            result_data={"exit_code": 0, "duration_seconds": 0.1},
            result_ok=True,
            change_revision=2,
        )

        self.assertFalse(failed.environment_error)
        self.assertEqual(
            VerificationTracker.evaluate(
                [failed, passed], change_revision=2, had_file_modification=True
            ),
            VerificationStatus.FAILED,
        )


if __name__ == "__main__":
    unittest.main()
