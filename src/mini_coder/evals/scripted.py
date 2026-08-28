from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from ..messages import ModelResponse
from ..model import ModelClient


class ScriptedModel(ModelClient):
    """Deterministic model used by offline evals and CI."""

    def __init__(self, responses: Sequence[ModelResponse | BaseException]) -> None:
        self._responses = list(responses)
        self.requests: list[tuple[list[dict[str, Any]], list[dict[str, Any]]]] = []

    def complete(
        self,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]],
    ) -> ModelResponse:
        self.requests.append((list(messages), list(tools)))
        if not self._responses:
            raise AssertionError("scripted eval model ran out of responses")
        response = self._responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class SharedScriptedModel(ScriptedModel):
    """A scripted model whose remaining responses survive runner recreation."""

    def remaining(self) -> list[ModelResponse | BaseException]:
        return self._responses
