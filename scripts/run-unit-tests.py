from __future__ import annotations

import sys
import unittest


def _workflow_escape(value: str) -> str:
    """Escape text embedded in a GitHub Actions workflow command."""
    return (
        value.replace("%", "%25")
        .replace("\r", "%0D")
        .replace("\n", "%0A")
    )


def main() -> int:
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
