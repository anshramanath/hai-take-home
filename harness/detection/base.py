"""The Detector contract (section 7)."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Protocol

from harness.scheduling.clock import Clock


@dataclass(frozen=True)
class AttentionItem:
    detector: str
    dedupe_key: str
    owner_id: str | None
    summary: str
    facts: dict[str, Any]


@dataclass(frozen=True)
class DetectionContext:
    conn: sqlite3.Connection
    clock: Clock


class Detector(Protocol):
    name: str

    def detect(self, ctx: DetectionContext) -> list[AttentionItem]: ...
