import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import todo_cli


class TodoCliTests(unittest.TestCase):
    def test_add_list_and_done(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "todo.json"
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(todo_cli.main(["add", "first", "--file", str(path)]), 0)
                self.assertEqual(todo_cli.main(["add", "second", "--file", str(path)]), 0)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(todo_cli.main(["list", "--file", str(path)]), 0)
            self.assertIn("1 [ ] first", output.getvalue())
            self.assertIn("2 [ ] second", output.getvalue())
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(todo_cli.main(["done", "1", "--file", str(path)]), 0)
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertTrue(data["items"][0]["done"])

    def test_ids_are_not_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "todo.json"
            path.write_text('{"items":[{"id":4,"text":"old","done":true}]}', encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(todo_cli.main(["add", "new", "--file", str(path)]), 0)
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["items"][-1]["id"], 5)
