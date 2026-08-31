from __future__ import annotations

import subprocess
from pathlib import Path
from urllib.parse import urlparse


NETWORK_NAME = "claw-benchmark-internal"
PROXY_NAME = "claw-benchmark-api-proxy"
PROXY_IMAGE = (
    "python:3.12-alpine@"
    "sha256:d09d15e60962ca365d1cd544a48773bac9d33f2fb1b00f2aa0deec78ade7dc31"
)
PROXY_ALIAS = "claw-api-proxy"


def provider_host(base_url: str) -> str:
    parsed = urlparse(base_url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("formal benchmark base URL must be an https URL")
    return parsed.hostname


def _run(command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
    )
    if check and completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(
            f"Docker command failed ({command[0]} {command[1]}): {detail}"
        )
    return completed


def ensure_network_guard(base_url: str) -> tuple[str, str]:
    """Create an internal agent network with one HTTPS CONNECT allowlist proxy."""

    host = provider_host(base_url)
    network = _run(["docker", "network", "inspect", NETWORK_NAME], check=False)
    if network.returncode != 0:
        _run(["docker", "network", "create", "--internal", NETWORK_NAME])

    _run(["docker", "rm", "-f", PROXY_NAME], check=False)
    proxy_script = Path(__file__).with_name("allowlist_proxy.py").resolve()
    created = _run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            PROXY_NAME,
            "--network",
            "bridge",
            "-e",
            f"ALLOWED_HOSTS={host}",
            "-v",
            f"{proxy_script}:/opt/claw/allowlist_proxy.py:ro",
            PROXY_IMAGE,
            "python",
            "/opt/claw/allowlist_proxy.py",
        ]
    )
    if not created.stdout.strip():
        raise RuntimeError("restricted API proxy did not start")
    _run(
        [
            "docker",
            "network",
            "connect",
            "--alias",
            PROXY_ALIAS,
            NETWORK_NAME,
            PROXY_NAME,
        ]
    )
    return NETWORK_NAME, f"http://{PROXY_ALIAS}:3128"
