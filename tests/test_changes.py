from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mini_coder.changes import ChangeTracker
from mini_coder.exceptions import ChangeConflictError, ChangeError


class ChangeTrackerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name)
        self.tracker = ChangeTracker(self.workspace)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_new_file_diff_and_apply(self) -> None:
        prepared = self.tracker.prepare(
            "write_file",
            {"path": "new.txt", "content": "alpha\nbeta\n"},
            "execution-new",
        )

        self.assertIsNone(prepared.before_hash)
        self.assertIn("--- /dev/null", prepared.unified_diff)
        self.assertIn("+++ b/new.txt", prepared.unified_diff)
        self.assertEqual(prepared.additions, 2)
        self.assertEqual(prepared.deletions, 0)

        change = self.tracker.apply(prepared)

        self.assertEqual((self.workspace / "new.txt").read_text(encoding="utf-8"), "alpha\nbeta\n")
        self.assertEqual(change.after_hash, prepared.after_hash)
        self.assertEqual(change.tool_execution_id, "execution-new")

    def test_empty_new_file_has_diff_headers_and_noop_edit_is_rejected(self) -> None:
        empty = self.tracker.prepare(
            "write_file",
            {"path": "empty.txt", "content": ""},
            "execution-empty",
        )
        self.assertEqual(empty.unified_diff, "--- /dev/null\n+++ b/empty.txt\n")
        self.tracker.apply(empty)

        path = self.workspace / "same.txt"
        path.write_text("same\n", encoding="utf-8")
        with self.assertRaisesRegex(ChangeError, "would not change"):
            self.tracker.prepare(
                "edit_file",
                {"path": "same.txt", "old_text": "same", "new_text": "same"},
                "execution-noop",
            )

    def test_edit_diff_records_deleted_lines(self) -> None:
        path = self.workspace / "example.txt"
        path.write_text("one\ntwo\nthree\n", encoding="utf-8")

        prepared = self.tracker.prepare(
            "edit_file",
            {
                "path": "example.txt",
                "old_text": "two\nthree\n",
                "new_text": "replacement\n",
            },
            "execution-edit",
        )

        self.assertEqual(prepared.additions, 1)
        self.assertEqual(prepared.deletions, 2)
        self.assertIn("-two", prepared.unified_diff)
        self.assertIn("-three", prepared.unified_diff)
        self.assertIn("+replacement", prepared.unified_diff)

        self.tracker.apply(prepared)
        self.assertEqual(path.read_text(encoding="utf-8"), "one\nreplacement\n")

    def test_large_diff_is_explicitly_truncated(self) -> None:
        tracker = ChangeTracker(self.workspace, max_diff_chars=160)
        prepared = tracker.prepare(
            "write_file",
            {"path": "large.txt", "content": "".join(f"line-{i}\n" for i in range(100))},
            "execution-large",
        )

        self.assertTrue(prepared.diff_truncated)
        self.assertIn("diff truncated", prepared.unified_diff)
        self.assertLessEqual(len(prepared.unified_diff), 160)
        self.assertEqual(prepared.additions, 100)

    def test_apply_rejects_external_modification_after_prepare(self) -> None:
        path = self.workspace / "conflict.txt"
        path.write_text("before\n", encoding="utf-8")
        prepared = self.tracker.prepare(
            "edit_file",
            {"path": "conflict.txt", "old_text": "before", "new_text": "agent"},
            "execution-conflict",
        )
        path.write_text("user change\n", encoding="utf-8")

        with self.assertRaisesRegex(ChangeConflictError, "changed after approval"):
            self.tracker.apply(prepared)

        self.assertEqual(path.read_text(encoding="utf-8"), "user change\n")

    def test_atomic_replace_failure_preserves_original(self) -> None:
        path = self.workspace / "atomic.txt"
        path.write_text("original\n", encoding="utf-8")
        prepared = self.tracker.prepare(
            "edit_file",
            {"path": "atomic.txt", "old_text": "original", "new_text": "updated"},
            "execution-atomic",
        )

        with patch("mini_coder.changes.tracker.os.replace", side_effect=OSError("blocked")):
            with self.assertRaisesRegex(ChangeError, "Atomic write failed"):
                self.tracker.apply(prepared)

        self.assertEqual(path.read_text(encoding="utf-8"), "original\n")
        self.assertEqual(list(self.workspace.glob("*.tmp")), [])

    def test_single_and_reverse_order_undo(self) -> None:
        path = self.workspace / "history.txt"
        path.write_text("A\n", encoding="utf-8")
        first = self.tracker.apply(
            self.tracker.prepare(
                "edit_file",
                {"path": "history.txt", "old_text": "A", "new_text": "B"},
                "execution-first",
            )
        )
        second = self.tracker.apply(
            self.tracker.prepare(
                "edit_file",
                {"path": "history.txt", "old_text": "B", "new_text": "C"},
                "execution-second",
            )
        )
        changes = [first, second]

        undone_second, second_event = self.tracker.undo_last(changes)
        self.assertEqual(undone_second.change_id, second.change_id)
        self.assertEqual(second_event.change_id, second.change_id)
        self.assertEqual(path.read_text(encoding="utf-8"), "B\n")

        undone_first, _ = self.tracker.undo_last(changes)
        self.assertEqual(undone_first.change_id, first.change_id)
        self.assertEqual(path.read_text(encoding="utf-8"), "A\n")

    def test_undo_change_targets_one_file_without_touching_another(self) -> None:
        first = self.tracker.apply(
            self.tracker.prepare(
                "write_file",
                {"path": "first.txt", "content": "first\n"},
                "execution-first-file",
            )
        )
        second = self.tracker.apply(
            self.tracker.prepare(
                "write_file",
                {"path": "second.txt", "content": "second\n"},
                "execution-second-file",
            )
        )

        undone, event = self.tracker.undo_change([first, second], first.change_id)

        self.assertEqual(undone.change_id, first.change_id)
        self.assertEqual(event.change_id, first.change_id)
        self.assertFalse((self.workspace / "first.txt").exists())
        self.assertTrue((self.workspace / "second.txt").exists())
        self.assertEqual(second.undo_status, "active")

    def test_undo_new_file_removes_it(self) -> None:
        change = self.tracker.apply(
            self.tracker.prepare(
                "write_file",
                {"path": "created.txt", "content": "created\n"},
                "execution-created",
            )
        )

        _, undo = self.tracker.undo_last([change])

        self.assertFalse((self.workspace / "created.txt").exists())
        self.assertIsNone(undo.restored_hash)

    def test_undo_conflict_preserves_user_change(self) -> None:
        path = self.workspace / "undo-conflict.txt"
        path.write_text("before\n", encoding="utf-8")
        change = self.tracker.apply(
            self.tracker.prepare(
                "edit_file",
                {"path": path.name, "old_text": "before", "new_text": "agent"},
                "execution-undo-conflict",
            )
        )
        path.write_text("user after agent\n", encoding="utf-8")

        with self.assertRaisesRegex(ChangeConflictError, "file changed after the Agent write"):
            self.tracker.undo_last([change])

        self.assertEqual(path.read_text(encoding="utf-8"), "user after agent\n")
        self.assertEqual(change.undo_status, "active")

    def test_preserves_crlf_and_utf8_bom(self) -> None:
        path = self.workspace / "windows.txt"
        path.write_bytes(b"\xef\xbb\xbffirst\r\nsecond\r\n")
        prepared = self.tracker.prepare(
            "edit_file",
            {
                "path": path.name,
                "old_text": "first\nsecond",
                "new_text": "alpha\nbeta",
            },
            "execution-newline",
        )

        self.assertEqual(prepared.encoding, "utf-8-sig")
        self.assertEqual(prepared.newline, "\r\n")
        self.tracker.apply(prepared)
        self.assertEqual(path.read_bytes(), b"\xef\xbb\xbfalpha\r\nbeta\r\n")

    def test_rejects_binary_and_oversized_files(self) -> None:
        (self.workspace / "binary.dat").write_bytes(b"abc\x00def")
        with self.assertRaisesRegex(ChangeError, "Binary files"):
            self.tracker.prepare(
                "edit_file",
                {"path": "binary.dat", "old_text": "a", "new_text": "b"},
                "execution-binary",
            )

        tracker = ChangeTracker(self.workspace, max_file_bytes=4)
        (self.workspace / "large.txt").write_text("12345", encoding="utf-8")
        with self.assertRaisesRegex(ChangeError, "exceeds 4 bytes"):
            tracker.prepare(
                "edit_file",
                {"path": "large.txt", "old_text": "1", "new_text": "2"},
                "execution-oversized",
            )

    def test_rejects_symbolic_link_write_targets(self) -> None:
        original = Path.is_symlink

        def fake_is_symlink(path: Path) -> bool:
            return path.name == "link.txt" or original(path)

        with patch.object(Path, "is_symlink", autospec=True, side_effect=fake_is_symlink):
            with self.assertRaisesRegex(ChangeError, "Symbolic links are not supported"):
                self.tracker.prepare(
                    "write_file",
                    {"path": "link.txt", "content": "data"},
                    "execution-symlink",
                )


if __name__ == "__main__":
    unittest.main()
