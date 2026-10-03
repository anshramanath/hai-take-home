"""LLM clients (section 10). `LLMClient` is the interface everything else
codes against; `complete()` either returns a validated instance of
`response_model` or raises `LLMOutputInvalid`, uniformly across
implementations, so callers (the workflow's bounded steps, the planner)
never need to know which client they're holding.

`FakeLLMClient` was pulled forward to phase 3 because the workflow
engine's bounded steps needed it before the rest of planning/ existed.
`OpenAIClient` and `ReplayClient` are phase 4: the real API call, and a
client that replays responses recorded from a real run so the demo works
without a key.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Protocol, Type, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class LLMOutputInvalid(Exception):
    """Raised by any LLMClient when the model's output could not be
    validated against the requested response_model, or when the API
    refused to produce structured output at all.
    """


class LLMClient(Protocol):
    def complete(self, messages: list[dict[str, str]], response_model: Type[T]) -> T: ...


class FakeLLMClient:
    """Scripted responses, consumed in order. An entry may be a BaseModel
    (returned) or an Exception instance (raised) — scripting a raised
    LLMOutputInvalid is how tests simulate a model producing bad output.
    No network call is ever reachable through this class.

    A scripted response is re-validated against whatever response_model is
    actually requested, rather than checked with isinstance: the planner
    asks for a narrowed response model built fresh per call (its
    WorkflowRequest.workflow field is pinned to a Literal of whatever is
    currently registered, built in planner.py), so the exact class the
    planner requests is not one a test can import and construct ahead of
    time. Scripting a plain `PlannerOutput(proposal=WorkflowRequest(...))`
    still works: it's re-validated into the dynamic variant actually
    asked for. A genuine mismatch (the wrong shape entirely) still raises.
    """

    def __init__(self, responses: list[BaseModel | Exception]):
        self._responses: list[BaseModel | Exception] = list(responses)
        self.calls: list[tuple[list[dict[str, str]], Type[BaseModel]]] = []

    def complete(self, messages: list[dict[str, str]], response_model: Type[T]) -> T:
        self.calls.append((messages, response_model))
        if not self._responses:
            raise RuntimeError("FakeLLMClient: no more scripted responses")
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        try:
            return response_model.model_validate(response.model_dump())
        except Exception as exc:
            raise TypeError(
                f"scripted response {type(response).__name__} does not validate against "
                f"requested {response_model.__name__}: {exc}"
            ) from exc


class OpenAIClient:
    """Structured outputs via the Chat Completions API. Model name comes
    from HARNESS_MODEL (default gpt-4o-mini); the API key is read by the
    OpenAI SDK itself from OPENAI_API_KEY, never handled by this class.

    Builds the JSON schema request by hand with "strict": False rather than
    using the SDK's `.parse()` convenience wrapper, which forces strict
    mode. Strict mode requires every object in the schema, including
    nested ones, to declare `additionalProperties: false` with a fixed set
    of keys — which `ToolCall.args` and `WorkflowRequest.params`
    (genuinely free-form dicts, since different tools and workflows take
    different arguments) cannot do. Pydantic's own `extra="forbid"`
    validation still runs on the response afterward, so this does not
    trade away correctness: a field that should be closed still is, by the
    time code ever sees a parsed object.
    """

    def __init__(self, model: str | None = None):
        from openai import OpenAI

        self._client = OpenAI()
        self._model = model or os.environ.get("HARNESS_MODEL", "gpt-4o-mini")

    def complete(self, messages: list[dict[str, str]], response_model: Type[T]) -> T:
        schema = response_model.model_json_schema()
        try:
            completion = self._client.chat.completions.create(
                model=self._model,
                messages=messages,
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": response_model.__name__, "schema": schema, "strict": False},
                },
            )
        except Exception as exc:
            raise LLMOutputInvalid(str(exc)) from exc

        message = completion.choices[0].message
        if message.refusal:
            raise LLMOutputInvalid(message.refusal)
        if not message.content:
            raise LLMOutputInvalid("model returned no content")
        try:
            return response_model.model_validate_json(message.content)
        except Exception as exc:
            raise LLMOutputInvalid(str(exc)) from exc


class ReplayClient:
    """Replays responses recorded from a real OpenAIClient run, so the demo
    and the recorded-run transcript work without an API key. The file is a
    JSON array of plain objects; each `complete()` call validates the next
    one against whatever response_model it's asked for, in order — the
    same sequential assumption FakeLLMClient makes, since a replay is only
    ever played back against the exact call sequence it was recorded from.
    """

    def __init__(self, path: Path | str):
        self._path = Path(path)
        data = json.loads(self._path.read_text())
        if not isinstance(data, list):
            raise ValueError(f"replay file {self._path} must contain a JSON array")
        self._responses: list[dict] = data
        self._index = 0

    def complete(self, messages: list[dict[str, str]], response_model: Type[T]) -> T:
        if self._index >= len(self._responses):
            raise LLMOutputInvalid(f"ReplayClient: no recorded response left in {self._path}")
        raw = self._responses[self._index]
        self._index += 1
        try:
            return response_model.model_validate(raw)
        except Exception as exc:
            raise LLMOutputInvalid(str(exc)) from exc


class RecordingLLMClient:
    """Wraps a real LLMClient (OpenAIClient) and records every parsed
    response as a plain dict, in call order. Used once, by hand, to
    produce the `runs/scenario_a_responses.json` replay fixture from an
    actual run against the real API — not used by the harness itself at
    runtime, and never touched by any test.
    """

    def __init__(self, inner: LLMClient):
        self._inner = inner
        self.recorded: list[dict] = []

    def complete(self, messages: list[dict[str, str]], response_model: Type[T]) -> T:
        result = self._inner.complete(messages, response_model)
        self.recorded.append(json.loads(result.model_dump_json()))
        return result
