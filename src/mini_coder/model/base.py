from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Any

from ..messages import ModelResponse


class ModelClient(ABC):
    """Vendor-neutral boundary used by the local agent loop."""

    @abstractmethod
    def complete(
        self,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]],
    ) -> ModelResponse:
        """Return one assistant turn, including zero or more tool calls."""

