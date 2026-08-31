from __future__ import annotations

import sys
import unittest
from pathlib import Path


def _workflow_escape(value: str) -> str:
    """Escape text embedded in a GitHub Actions workflow command."""
    return (
        value.replace("%", "%25")
        .replace("\r", "%0D")
        .replace("\n", "%0A")
    )


def main() -> int:
    repository_root = Path(__file__).resolve().parents[1]
    if str(repository_root) not in sys.path:
        sys.path.insert(0, str(repository_root))
    suite = unittest.defaultTestLoader.discover("tests")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if result.wasSuccessful():
        return 0

    for test, traceback in [*result.failures, *result.errors]:
        test_id = test.id()
        detail = traceback[-3_000:]
        print(
            f"::error title={_workflow_escape(test_id)}::"
            f"{_workflow_escape(detail)}",
            flush=True,
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
