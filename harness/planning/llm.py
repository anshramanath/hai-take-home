"""The LLM client interface (section 10), pulled forward from phase 4:
the workflow engine's two bounded steps (choose_supplier,
draft_notification) need to call an LLM, so the interface and a fake
implementation have to exist now. OpenAIClient and ReplayClient, which need
real prompt text and API wiring, arrive in phase 4 with the free-form
planner.
"""

from __future__ import annotations

from typing import Protocol, Type, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class LLMClient(Protocol):
    def complete(self, messages: list[dict[str, str]], response_model: Type[T]) -> T: ...


class FakeLLMClient:
    """Scripted responses, consumed in order. Used by every test; no
    network call is ever reachable through this class.
    """

    def __init__(self, responses: list[BaseModel]):
        self._responses = list(responses)
        self.calls: list[tuple[list[dict[str, str]], Type[BaseModel]]] = []

    def complete(self, messages: list[dict[str, str]], response_model: Type[T]) -> T:
        self.calls.append((messages, response_model))
        if not self._responses:
            raise RuntimeError("FakeLLMClient: no more scripted responses")
        response = self._responses.pop(0)
        if not isinstance(response, response_model):
            raise TypeError(
                f"scripted response {type(response).__name__} does not match "
                f"requested {response_model.__name__}"
            )
        return response
