"""User lookups shared by the gate, approvals, and (in later phases) the
context providers. Users are company data, same as parts or suppliers, so
this lives in world/ alongside them.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class User:
    user_id: str
    name: str
    email: str
    role: str
    manager_id: str | None
    backup_approver_id: str | None
    scopes: frozenset[str]
    approval_limits: dict[str, float] | None


class UnknownUser(Exception):
    pass


def get_user(conn: sqlite3.Connection, user_id: str) -> User:
    row = conn.execute(
        "SELECT user_id, name, email, role, manager_id, backup_approver_id, scopes, "
        "approval_limits FROM users WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    if row is None:
        raise UnknownUser(user_id)
    return User(
        user_id=row[0],
        name=row[1],
        email=row[2],
        role=row[3],
        manager_id=row[4],
        backup_approver_id=row[5],
        scopes=frozenset(json.loads(row[6])),
        approval_limits=json.loads(row[7]) if row[7] is not None else None,
    )


def missing_scopes(user: User, required: tuple[str, ...]) -> list[str]:
    return [scope for scope in required if scope not in user.scopes]
