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
    r"git\s+branch\s+(?:-d|-D|--delete(?:\s+--force)?)|"
    r"git\s+diff\s+.*--output(?:=\S+|\s+\S+)|"
    r"drop\s+(?:database|table)|truncate\s+table)(?:\s|$)"
)
_EXTERNAL = re.compile(
    r"(?i)(?:^|\s)(?:curl|wget|invoke-webrequest|invoke-restmethod|ssh|scp|sftp|"
    r"rsync|gh\s+|git\s+(?:push|pull|fetch|clone)|docker\s+(?:push|pull|login)|"
    r"(?:python\s+-m\s+)?pip\s+(?:install|download)|pip-audit|"
    r"npm\s+(?:install|ci|publish|audit)|pnpm\s+(?:add|install|publish|audit)|"
    r"yarn\s+(?:add|install|publish|audit)|bun\s+(?:add|install|publish)|"
    r"cargo\s+(?:install|publish|audit)|gem\s+(?:install|push)|"
    r"composer\s+(?:install|update)|go\s+(?:get|install)|"
    r"dotnet\s+(?:restore|add\s+package|nuget\s+push)|"
    r"terraform\s+(?:init|plan|apply|destroy)|tofu\s+(?:init|plan|apply|destroy)|"
    r"helm\s+(?:install|upgrade|push|repo)|kubectl\s+|"
    r"apt(?:-get)?\s+|brew\s+install|choco\s+install|winget\s+install|"
    r"publish|deploy)(?:\s|$)"
)
_WORKSPACE_WRITE = re.compile(
    r"(?i)(?:^|\s)(?:git\s+(?:add|commit|checkout|switch|merge|rebase|restore|stash)|"
    r"mkdir|md\s+|touch|"
    r"(?:python(?:\.exe)?\s+-m\s+)?(?:unittest|pytest|doctest|compileall|py_compile)|"
    r"py\.test|tox|nox|ruff\s+(?:check|format)|black|isort|flake8|pylint|"
    r"mypy|pyright|basedpyright|bandit|"
    r"(?:npm|pnpm|yarn|bun)\s+(?:test|run\s+(?:test|build|lint|check|typecheck|format)"
    r"(?::[\w.-]+)?)|vitest|jest|eslint|stylelint|tsc|biome\s+(?:check|lint)|"
    r"prettier\s+(?:--check|--list-different)|deno\s+(?:test|check|lint|fmt\s+--check)|"
    r"go\s+(?:test|vet)|golangci-lint\s+run|staticcheck|"
    r"cargo\s+(?:test|check|clippy|build|fmt)|"
    r"dotnet\s+(?:test|build|format)|"
    r"(?:mvn|mvnw)(?:\.cmd|\.bat)?\s+(?:(?:-[^\s]+)\s+)*(?:test|verify|package|compile)|"
    r"(?:gradle|gradlew)(?:\.cmd|\.bat)?\s+(?:(?:-[^\s]+)\s+)*(?:test|check|build|assemble)|"
    r"ant\s+(?:test|check|build)|make\s+(?:test|check|build|lint)|"
    r"ctest|cmake\s+--build|ninja\s+(?:test|check)|meson\s+(?:test|compile)|"
    r"javac|(?:bundle\s+exec\s+)?rspec|rake\s+(?:test|spec)|"
    r"phpunit|phpstan\s+analyse|psalm|composer\s+(?:test|run\s+test)|"
    r"swift\s+(?:test|build)|dart\s+(?:test|analyze)|"
    r"flutter\s+(?:test|analyze)|mix\s+(?:test|compile|format)|"
    r"rebar3\s+(?:eunit|ct|compile)|zig\s+(?:test|build)|"
    r"shellcheck|shfmt\s+-d|markdownlint|markdownlint-cli|rstcheck|vale|"
    r"yamllint|taplo\s+check|actionlint|hadolint|"
    r"docker\s+compose\s+config|(?:terraform|tofu)\s+(?:validate|fmt\s+-check)|"
    r"helm\s+(?:lint|template)|mkdocs\s+build|sphinx-build)"
    r"(?:\s|$)"
)
_READ_ONLY = re.compile(
    r"(?i)^\s*(?:python(?:\.exe)?\s+--version|python(?:\.exe)?\s+-V|"
    r"(?:python(?:\.exe)?\s+-m\s+)?pip\s+(?:show|list|freeze|check)|"
    r"(?:node|npm|pnpm|yarn|bun|deno|go|cargo|rustc|dotnet|java|javac|"
    r"ruby|php|perl|swift|dart|flutter|zig)\s+(?:--version|-V|version)|"
    r"node\s+(?:--check|-c)|ruby\s+-c|php\s+-l|perl\s+-c|(?:bash|sh)\s+-n|"
    r"(?:gcc|g\+\+|clang|clang\+\+)\s+.*(?:-fsyntax-only)|"
    r"git\s+(?:status|diff|log|show)|"
    r"git\s+branch(?:\s+(?:--list|-a|-r|-v|-vv|--show-current|--contains)(?:\s+[^\s]+)?)?|"
    r"pwd|cd|dir|ls|where|which|whoami)(?:\s|$)"
)
_COMPOUND = re.compile(r"(?:[;&|<>`\r\n]|\$\()")


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
