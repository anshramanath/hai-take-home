"""Section 15.15: the demo smoke test. Uses ReplayClient (never the real
API — section 15's "no network in tests" rule applies here too), asserts
it runs to completion and its output contains every section header and a
handful of key facts from each section.
"""

from __future__ import annotations

from pathlib import Path

from rich.console import Console

from harness.demo import run_demo
from harness.planning.llm import ReplayClient

REPLAY_PATH = Path(__file__).resolve().parent.parent / "runs" / "scenario_a_responses.json"


def test_demo_runs_to_completion_with_replay_and_contains_key_facts():
    assert REPLAY_PATH.exists(), "runs/scenario_a_responses.json must exist for this test"

    console = Console(record=True, width=120)
    llm_client = ReplayClient(REPLAY_PATH)

    run_demo(console, llm_client, interactive=False)  # must not raise, must not block on input

    text = console.export_text()

    section_headers = [
        "Scenario A: at-risk part, supplier delay, reroute",
        "Follow-up: confirming the replacement arrived",
        "Scenario B: quality hold, lot reallocation (covers)",
        "Scenario B: quality hold, no covering lot (shortage)",
        "Failure cases",
        "Explain: Scenario A, from the audit log alone",
    ]
    for header in section_headers:
        assert header in text, f"missing section header: {header!r}"

    key_facts = [
        "Escalated",
        "Reallocated",
        "Shortage flagged",
        "No qualifying supplier",
        "Over-limit routing",
        "Missing scope blocked",
        "Simulated crash",
        "Duplicate detection",
        "Tampered plan rejected",
        "Arrival not received",
        "PO-77812",
    ]
    for fact in key_facts:
        assert fact in text, f"missing key fact: {fact!r}"
