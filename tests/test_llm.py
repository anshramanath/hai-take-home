from __future__ import annotations

import json

import pytest
from pydantic import BaseModel, ConfigDict

from harness.planning.llm import FakeLLMClient, LLMOutputInvalid, ReplayClient


class _ResponseA(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str


class _ResponseB(BaseModel):
    model_config = ConfigDict(extra="forbid")
    different_required_field: int


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
    # The check is structural (re-validation), not nominal (isinstance), so
    # the two models must actually differ in shape to prove a mismatch is
    # still caught.
    client = FakeLLMClient([_ResponseA(value="wrong type")])
    with pytest.raises(TypeError):
        client.complete([], _ResponseB)


def test_replay_client_returns_recorded_responses_in_order(tmp_path):
    path = tmp_path / "replay.json"
    path.write_text(json.dumps([{"value": "first"}, {"value": "second"}]))

    client = ReplayClient(path)
    assert client.complete([], _ResponseA).value == "first"
    assert client.complete([], _ResponseA).value == "second"


def test_replay_client_raises_when_recording_is_exhausted(tmp_path):
    path = tmp_path / "replay.json"
    path.write_text(json.dumps([{"value": "only"}]))

    client = ReplayClient(path)
    client.complete([], _ResponseA)
    with pytest.raises(LLMOutputInvalid):
        client.complete([], _ResponseA)


def test_replay_client_raises_on_invalid_recorded_response(tmp_path):
    path = tmp_path / "replay.json"
    path.write_text(json.dumps([{"different_required_field": 1}]))

    client = ReplayClient(path)
    with pytest.raises(LLMOutputInvalid):
        client.complete([], _ResponseA)


def test_replay_client_rejects_a_non_array_file(tmp_path):
    path = tmp_path / "replay.json"
    path.write_text(json.dumps({"not": "a list"}))

    with pytest.raises(ValueError):
        ReplayClient(path)
