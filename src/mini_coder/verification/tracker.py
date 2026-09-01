from __future__ import annotations

import re
from typing import Any

from .models import VerificationRecord, VerificationStatus


_VERIFICATION_PATTERNS = (
    r"(?:^|\s)(?:python(?:\.exe)?\s+-m\s+)?(?:unittest|pytest|doctest|compileall|py_compile)(?:\s|$)",
    r"(?:^|\s)(?:py\.test|tox|nox|ruff\s+(?:check|format)|black|isort|flake8|pylint|mypy|pyright|basedpyright|bandit)(?:\s|$)",
    r"(?:^|\s)(?:node\s+(?:--check|-c)|vitest|jest|eslint|stylelint|tsc|biome\s+(?:check|lint)|prettier\s+(?:--check|--list-different))(?:\s|$)",
    r"(?:^|\s)(?:npm|pnpm|yarn|bun)\s+(?:test|run\s+(?:test|build|lint|check|typecheck|format)(?::[\w.-]+)?)(?:\s|$)",
    r"(?:^|\s)deno\s+(?:test|check|lint|fmt\s+--check)(?:\s|$)",
    r"(?:^|\s)cargo\s+(?:test|check|clippy|build|fmt)(?:\s|$)",
    r"(?:^|\s)go\s+(?:test|vet)(?:\s|$)",
    r"(?:^|\s)(?:golangci-lint\s+run|staticcheck)(?:\s|$)",
    r"(?:^|\s)dotnet\s+(?:test|build|format)(?:\s|$)",
    r"(?:^|\s)(?:mvn|mvnw|gradle|gradlew)(?:\.cmd|\.bat)?\s+.*(?:test|verify|check|build|package|assemble)(?:\s|$)",
    r"(?:^|\s)(?:ant|make)\s+(?:test|check|build|lint)(?:\s|$)",
    r"(?:^|\s)(?:ctest|cmake\s+--build|ninja\s+(?:test|check)|meson\s+(?:test|compile)|javac)(?:\s|$)",
    r"(?:^|\s)(?:(?:bundle\s+exec\s+)?rspec|rake\s+(?:test|spec)|phpunit|phpstan\s+analyse|psalm|composer\s+(?:test|run\s+test))(?:\s|$)",
    r"(?:^|\s)(?:swift\s+(?:test|build)|dart\s+(?:test|analyze)|flutter\s+(?:test|analyze)|mix\s+(?:test|compile|format)|rebar3\s+(?:eunit|ct|compile)|zig\s+(?:test|build))(?:\s|$)",
    r"(?:^|\s)(?:shellcheck|shfmt\s+-d|markdownlint|markdownlint-cli|rstcheck|vale|yamllint|taplo\s+check|actionlint|hadolint)(?:\s|$)",
    r"(?:^|\s)(?:docker\s+compose\s+config|(?:terraform|tofu)\s+(?:validate|fmt\s+-check)|helm\s+(?:lint|template)|mkdocs\s+build|sphinx-build)(?:\s|$)",
)

_DOCUMENTATION_SUFFIXES = {".md", ".markdown", ".rst"}
_DOCUMENTATION_COMMAND = re.compile(
    r"(?i)(?:markdown|markdownlint|mkdocs|sphinx|doctest|rstcheck|README)"
)
_DOMAIN_SUFFIXES = {
    "python": {".py", ".pyi", ".pyx"},
    "web": {".js", ".jsx", ".ts", ".tsx", ".vue", ".svelte", ".css", ".scss", ".html"},
    "docs": _DOCUMENTATION_SUFFIXES,
    "native": {
        ".c", ".cc", ".cpp", ".cxx", ".h", ".hpp", ".rs", ".go",
        ".java", ".kt", ".cs", ".rb", ".php", ".swift", ".dart",
        ".ex", ".exs", ".erl", ".hrl", ".zig", ".sh",
    },
}
_CONFIG_NAMES = {
    "pyproject.toml",
    "setup.cfg",
    "tox.ini",
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "cargo.toml",
    "go.mod",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "composer.json",
    "gemfile",
    "package.swift",
    "pubspec.yaml",
    "mix.exs",
    "rebar.config",
    "cmakelists.txt",
    "makefile",
    "docker-compose.yml",
    "compose.yaml",
}


class VerificationTracker:
    """Derive verification state from commands and workspace change revisions."""

    @staticmethod
    def is_verification_command(arguments: dict[str, Any]) -> bool:
        purpose = arguments.get("purpose")
        if purpose == "verify":
            return True
        if purpose in {"inspect", "other"}:
            return False
        command = str(arguments.get("command", "")).strip().casefold()
        return any(re.search(pattern, command) for pattern in _VERIFICATION_PATTERNS)

    @staticmethod
    def record(
        *,
        tool_execution_id: str,
        arguments: dict[str, Any],
        result_data: dict[str, Any],
        result_ok: bool,
        change_revision: int,
        summary_limit: int = 800,
    ) -> VerificationRecord:
        expected_exit_codes = _expected_exit_codes(
            result_data.get("expected_exit_codes", arguments.get("expected_exit_codes"))
        )
        timed_out = bool(result_data.get("timed_out", False))
        expectation_met = bool(
            result_data.get(
                "expectation_met",
                result_data.get("exit_code") in expected_exit_codes,
            )
        ) and not timed_out
        verification_mode = str(
            result_data.get(
                "verification_mode",
                arguments.get("verification_mode", "standard"),
            )
        )
        if verification_mode not in {"standard", "expected_rejection"}:
            verification_mode = "standard"
        environment_error = bool(result_data.get("environment_error", False)) or _is_harness_error(
            command=str(arguments.get("command", "")),
            stderr=str(result_data.get("stderr", "")),
        )
        conclusive = verification_mode == "standard" and expected_exit_codes == (0,)
        return VerificationRecord.create(
            tool_execution_id=tool_execution_id,
            command=str(arguments.get("command", "")),
            cwd=str(arguments.get("cwd", ".")),
            exit_code=result_data.get("exit_code"),
            duration_seconds=float(result_data.get("duration_seconds", 0.0)),
            stdout_summary=_summary(result_data.get("stdout", ""), summary_limit),
            stderr_summary=_summary(result_data.get("stderr", ""), summary_limit),
            change_revision=change_revision,
            passed=bool(result_ok and expectation_met and not environment_error),
            timed_out=timed_out,
            expected_exit_codes=expected_exit_codes,
            expectation_met=expectation_met,
            verification_mode=verification_mode,
            conclusive=conclusive,
            environment_error=environment_error,
            scope_paths=_scope_paths(arguments.get("verification_paths")),
            scope_domains=_scope_domains(arguments),
        )

    @staticmethod
    def invalidate(
        records: list[VerificationRecord],
        *,
        reason: str,
        changed_path: str | None = None,
    ) -> list[VerificationRecord]:
        invalidated = []
        for record in records:
            if record.is_current and _change_affects_verification(record, changed_path):
                record.invalidate(reason)
                invalidated.append(record)
        return invalidated

    @staticmethod
    def evaluate(
        records: list[VerificationRecord],
        *,
        change_revision: int,
        had_file_modification: bool,
    ) -> VerificationStatus:
        current = [record for record in records if record.is_current]
        if current:
            # A retry of the same check supersedes its earlier result, while distinct
            # current checks all remain part of the acceptance gate.
            latest_by_check: dict[tuple[Any, ...], VerificationRecord] = {}
            for record in current:
                key = (
                    record.command,
                    record.cwd,
                    record.expected_exit_codes,
                    record.verification_mode,
                    record.scope_paths,
                    record.scope_domains,
                )
                latest_by_check[key] = record
            effective = list(latest_by_check.values())
            effective = [
                record
                for index, record in enumerate(effective)
                if not (
                    record.environment_error
                    and _later_success_covers_harness_error(record, effective[index + 1 :])
                )
            ]
            if any(not record.passed or record.environment_error for record in effective):
                return VerificationStatus.FAILED
            # A deliberate invalid-input rejection is useful supporting evidence,
            # but it cannot by itself prove the edited program works normally.
            if any(record.passed and record.conclusive for record in effective):
                return VerificationStatus.PASSED
            return (
                VerificationStatus.UNVERIFIED
                if had_file_modification
                else VerificationStatus.NOT_REQUIRED
            )
        if records:
            return VerificationStatus.STALE
        if had_file_modification:
            return VerificationStatus.UNVERIFIED
        return VerificationStatus.NOT_REQUIRED


def _summary(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 18)] + "...[truncated]"


def _is_harness_error(*, command: str, stderr: str) -> bool:
    """Recognize a malformed inline verifier without hiding target-code failures."""

    normalized_command = command.strip().casefold()
    normalized_stderr = stderr.casefold()
    return (
        bool(re.match(r"^(?:python(?:\.exe)?|py)\s+-c(?:\s|$)", normalized_command))
        and 'file "<string>"' in normalized_stderr
        and "syntaxerror: invalid syntax" in normalized_stderr
    )


def _later_success_covers_harness_error(
    failed: VerificationRecord,
    later: list[VerificationRecord],
) -> bool:
    """Allow a corrected verifier to replace only an explicit, identical scope."""

    if not failed.scope_paths:
        return False
    return any(
        record.passed
        and record.conclusive
        and not record.environment_error
        and record.change_revision == failed.change_revision
        and record.cwd == failed.cwd
        and record.scope_paths == failed.scope_paths
        and record.scope_domains == failed.scope_domains
        for record in later
    )


def _expected_exit_codes(value: Any) -> tuple[int, ...]:
    if not isinstance(value, (list, tuple)) or not value:
        return (0,)
    parsed = tuple(
        item for item in value if isinstance(item, int) and not isinstance(item, bool)
    )
    return parsed or (0,)


def _change_affects_verification(
    record: VerificationRecord,
    changed_path: str | None,
) -> bool:
    if not changed_path:
        return True
    normalized = changed_path.replace("\\", "/").lstrip("./").casefold()
    for scoped in record.scope_paths:
        scoped_normalized = scoped.replace("\\", "/").strip("./").casefold()
        if normalized == scoped_normalized or normalized.startswith(scoped_normalized + "/"):
            return True
    if record.scope_paths:
        return False
    domains = set(record.scope_domains)
    changed_domain = _path_domain(normalized)
    if changed_domain == "docs":
        return "docs" in domains or bool(_DOCUMENTATION_COMMAND.search(record.command))
    if changed_domain == "all":
        return True
    if "all" in domains:
        return True
    return changed_domain in domains


def _scope_paths(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(
        dict.fromkeys(
            item.replace("\\", "/").strip("./")
            for item in value
            if isinstance(item, str) and item.strip("./\\ ")
        )
    )


def _scope_domains(arguments: dict[str, Any]) -> tuple[str, ...]:
    explicit = arguments.get("verification_domains")
    allowed = {"python", "web", "docs", "native", "config", "all"}
    if isinstance(explicit, list):
        selected = tuple(
            dict.fromkeys(item for item in explicit if isinstance(item, str) and item in allowed)
        )
        if selected:
            return selected
    command = str(arguments.get("command", ""))
    lowered = command.casefold()
    if _DOCUMENTATION_COMMAND.search(command):
        return ("docs",)
    if re.search(r"(?:python|pytest|unittest|ruff|mypy|pyright|tox|nox)", lowered):
        return ("python", "config")
    if re.search(r"(?:node|npm|pnpm|yarn|bun|deno|vitest|jest|eslint|tsc|biome|prettier|stylelint)", lowered):
        return ("web", "config")
    if re.search(
        r"(?:cargo|go\s+(?:test|vet)|golangci|staticcheck|dotnet|mvn|gradle|"
        r"ant|make|cmake|ctest|ninja|meson|javac|rspec|rake|phpunit|phpstan|"
        r"psalm|swift|dart|flutter|mix|rebar3|zig|shellcheck)",
        lowered,
    ):
        return ("native", "config")
    return ("all",)


def _path_domain(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    if name in _CONFIG_NAMES:
        return "config"
    suffix = "." + name.rsplit(".", 1)[-1] if "." in name else ""
    for domain, suffixes in _DOMAIN_SUFFIXES.items():
        if suffix in suffixes:
            return domain
    return "all"
