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


if __name__ == "__main__":
    unittest.main()
