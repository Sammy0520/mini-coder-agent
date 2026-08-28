from __future__ import annotations

import argparse
import sys
import threading
import webbrowser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mini-coder-gui",
        description="Start the local Mini Coder Agent browser interface.",
    )
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Start the server without opening a browser tab",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not 1 <= args.port <= 65_535:
        print("Port must be between 1 and 65535.", file=sys.stderr)
        return 2
    try:
        import uvicorn

        from .app import create_app
    except ModuleNotFoundError as exc:
        if exc.name in {"fastapi", "uvicorn", "pydantic"}:
            print(
                'GUI dependencies are not installed. Run: python -m pip install -e ".[gui]"',
                file=sys.stderr,
            )
            return 2
        raise

    url = f"http://127.0.0.1:{args.port}"
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    print(f"Mini Coder Agent GUI: {url}")
    uvicorn.run(create_app(), host="127.0.0.1", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
