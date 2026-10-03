from __future__ import annotations

import pytest
from pydantic import BaseModel, ConfigDict

from harness.planning.llm import FakeLLMClient


class _ResponseA(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str


class _ResponseB(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str


def test_fake_llm_client_returns_scripted_responses_in_order():
    client = FakeLLMClient([_ResponseA(value="first"), _ResponseA(value="second")])
    assert client.complete([], _ResponseA).value == "first"
    assert client.complete([], _ResponseA).value == "second"
    assert len(client.calls) == 2


def test_fake_llm_client_raises_when_responses_are_exhausted():
    client = FakeLLMClient([_ResponseA(value="only")])
    client.complete([], _ResponseA)
    with pytest.raises(RuntimeError):
        client.complete([], _ResponseA)


def test_fake_llm_client_raises_on_response_model_mismatch():
    client = FakeLLMClient([_ResponseA(value="wrong type")])
    with pytest.raises(TypeError):
        client.complete([], _ResponseB)
