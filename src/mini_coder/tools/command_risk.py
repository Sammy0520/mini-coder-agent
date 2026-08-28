from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class CommandRisk(str, Enum):
    READ_ONLY = "read_only"
    WORKSPACE_WRITE = "workspace_write"
    EXTERNAL_EFFECT = "external_effect"
    DANGEROUS = "dangerous"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class CommandAssessment:
    level: CommandRisk
    summary: str
    expected_side_effects: str

    @property
    def auto_approvable(self) -> bool:
        return self.level in {CommandRisk.READ_ONLY, CommandRisk.WORKSPACE_WRITE}


_DANGEROUS = re.compile(
    r"(?i)(?:^|\s)(?:rm\s+(?:-[A-Za-z]*r[A-Za-z]*|--recursive)|rmdir|del\s+|"
    r"remove-item|format(?:\.com)?\s+|"
    r"copy|cp|move|mv|rename|ren|copy-item|move-item|rename-item|"
    r"set-content|out-file\s+|"
    r"diskpart|shutdown|reboot|git\s+reset\s+--hard|git\s+clean\s+-|"
    r"drop\s+(?:database|table)|truncate\s+table)(?:\s|$)"
)
_EXTERNAL = re.compile(
    r"(?i)(?:^|\s)(?:curl|wget|invoke-webrequest|invoke-restmethod|ssh|scp|sftp|"
    r"rsync|gh\s+|git\s+(?:push|pull|fetch|clone)|docker\s+(?:push|pull|login)|"
    r"(?:python\s+-m\s+)?pip\s+install|npm\s+(?:install|publish)|pnpm\s+install|"
    r"yarn\s+(?:add|install|publish)|apt(?:-get)?\s+|brew\s+install|choco\s+install|"
    r"winget\s+install|publish|deploy)(?:\s|$)"
)
_WORKSPACE_WRITE = re.compile(
    r"(?i)(?:^|\s)(?:git\s+(?:add|commit|checkout|switch|merge|rebase|restore|stash)|"
    r"mkdir|md\s+|touch|"
    r"black|prettier|isort|ruff\s+format|cargo\s+(?:build|fmt)|"
    r"dotnet\s+build|mvn\s+(?:test|verify|package)|gradle\w*\s+(?:test|check|build)|"
    r"npm\s+(?:test|run\s+(?:test|build|lint|check|typecheck))|"
    r"pnpm\s+(?:test|run)|yarn\s+(?:test|run)|go\s+test|cargo\s+(?:test|check|clippy)|"
    r"(?:python(?:\.exe)?\s+-m\s+)?(?:unittest|pytest)|py\.test|tox|nox|"
    r"vitest|jest|make\s+(?:test|check|build|lint)|ruff\s+check|mypy|pyright|eslint|tsc)"
    r"(?:\s|$)"
)
_READ_ONLY = re.compile(
    r"(?i)^\s*(?:python(?:\.exe)?\s+--version|python(?:\.exe)?\s+-V|"
    r"node\s+--version|npm\s+--version|git\s+(?:status|diff|log|show|branch)|"
    r"pwd|cd|dir|ls|where|which|whoami)(?:\s|$)"
)
_COMPOUND = re.compile(r"(?:&&|\|\||[;|<>\r\n])")


def assess_command(command: str) -> CommandAssessment:
    normalized = command.strip()
    if not normalized:
        return CommandAssessment(
            CommandRisk.UNKNOWN,
            "Empty command cannot be classified.",
            "No command should run.",
        )
    if _DANGEROUS.search(normalized) or re.search(r"(?m)(?:^|\s)>+\s*[^&]", normalized):
        return CommandAssessment(
            CommandRisk.DANGEROUS,
            "Command may delete, overwrite, or irreversibly reset data.",
            "Potential destructive changes inside or outside the workspace.",
        )
    if _COMPOUND.search(normalized):
        return CommandAssessment(
            CommandRisk.UNKNOWN,
            "Compound shell syntax is not classified reliably.",
            "Side effects may come from any component of the shell expression.",
        )
    if _EXTERNAL.search(normalized):
        return CommandAssessment(
            CommandRisk.EXTERNAL_EFFECT,
            "Command may access a network service or install/publish artifacts.",
            "External state, dependencies, credentials, or remote repositories may change.",
        )
    if _WORKSPACE_WRITE.search(normalized):
        return CommandAssessment(
            CommandRisk.WORKSPACE_WRITE,
            "Command is expected to test, build, format, or modify workspace state.",
            "Files, caches, build outputs, or local Git state may change in the workspace.",
        )
    if _READ_ONLY.search(normalized):
        return CommandAssessment(
            CommandRisk.READ_ONLY,
            "Known inspection command with no intended persistent change.",
            "Reads local process or repository information.",
        )
    return CommandAssessment(
        CommandRisk.UNKNOWN,
        "Command is not in the local risk model.",
        "Side effects cannot be predicted reliably; human confirmation is required.",
    )
