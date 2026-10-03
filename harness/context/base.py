"""The Provider contract (section 7, section 9). Every provider checks the
user's read scope first, filters to what that user may see, filters for
relevance to the attention item at hand, and returns record_ids for audit.
A user without the relevant scope always gets an empty slice back, never
an error: a provider's job is to decide what reaches the model, not to
police whether this call was expected to happen.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Protocol

from harness.detection.base import AttentionItem
from harness.scheduling.clock import Clock
from harness.world.users import User


@dataclass(frozen=True)
class ContextSlice:
    source: str
    records: list[dict[str, Any]]
    record_ids: list[str]


@dataclass(frozen=True)
class ProviderContext:
    conn: sqlite3.Connection
    clock: Clock


class Provider(Protocol):
    source: str

    def fetch(self, ctx: ProviderContext, user: User, item: AttentionItem) -> ContextSlice: ...
