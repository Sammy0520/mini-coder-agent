from __future__ import annotations

import re
import subprocess
from pathlib import Path


FORBIDDEN_NAMES = {"auth.json", ".env", "id_rsa", "id_ed25519"}
SECRET_PATTERNS = {
    "OpenAI-style API key": re.compile(r"\bsk-[A-Za-z0-9_-]{32,}\b"),
    "private key material": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
}


def candidate_files(repository: Path) -> list[Path]:
    completed = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=repository,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.decode("utf-8", errors="replace").strip())
    return [repository / raw.decode("utf-8") for raw in completed.stdout.split(b"\0") if raw]


def main() -> int:
    repository = Path(__file__).resolve().parent.parent
    findings: list[str] = []
    for path in candidate_files(repository):
        relative = path.relative_to(repository).as_posix()
        # During a cleanup commit, Git still reports tracked files that have
        # already been removed from the working tree. They cannot contain a
        # newly introduced secret, so do not turn a valid deletion into a
        # false-positive scan failure.
        if not path.exists():
            continue
        name = path.name.casefold()
        forbidden_environment = name.startswith(".env.") and name != ".env.example"
        if name in FORBIDDEN_NAMES or forbidden_environment:
            findings.append(f"forbidden credential filename: {relative}")
            continue
        try:
            data = path.read_bytes()
        except OSError as exc:
            findings.append(f"could not inspect {relative}: {exc}")
            continue
        if b"\x00" in data[:8192]:
            continue
        text = data.decode("utf-8", errors="replace")
        for label, pattern in SECRET_PATTERNS.items():
            if pattern.search(text):
                findings.append(f"{label}: {relative}")

    if findings:
        print("Potential committed secrets detected:")
        for finding in findings:
            print(f"- {finding}")
        return 1
    print("Tracked-file secret check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
