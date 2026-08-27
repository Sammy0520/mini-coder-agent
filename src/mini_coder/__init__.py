"""A compact coding agent whose orchestration is implemented locally."""

from .agent import AgentRunResult, AgentRunner
from .config import AgentConfig, ApprovalPolicy, WireAPI

__all__ = ["AgentConfig", "AgentRunResult", "AgentRunner", "ApprovalPolicy", "WireAPI"]
__version__ = "0.1.0"
