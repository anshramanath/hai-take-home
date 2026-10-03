"""CalendarProvider (section 9): the user's own events for the next 7
days. Used both as context shown to the planner (so it can reason about
the requester's own availability) and, separately and never shown to the
model, by the policy layer's escalation check on an approver's calendar
(section 9's "policy reads" distinction) — that second use goes straight
to cal_events, not through this provider.
"""

from __future__ import annotations

from datetime import date, timedelta

from harness.context.base import ContextSlice, ProviderContext
from harness.detection.base import AttentionItem
from harness.world.users import User

WINDOW_DAYS = 7


class CalendarProvider:
    source = "calendar"

    def fetch(self, ctx: ProviderContext, user: User, item: AttentionItem) -> ContextSlice:
        if "calendar:read" not in user.scopes:
            return ContextSlice(source=self.source, records=[], record_ids=[])

        today = ctx.clock.today()
        horizon = today + timedelta(days=WINDOW_DAYS)

        records: list[dict] = []
        record_ids: list[str] = []
        for row in ctx.conn.execute(
            "SELECT event_id, start, end, title, out_of_office FROM cal_events WHERE owner = ?",
            (user.user_id,),
        ).fetchall():
            start_date = date.fromisoformat(row["start"][:10])
            if not (today <= start_date <= horizon):
                continue
            records.append({
                "event_id": row["event_id"], "start": row["start"], "end": row["end"],
                "title": row["title"], "out_of_office": bool(row["out_of_office"]),
            })
            record_ids.append(row["event_id"])

        return ContextSlice(source=self.source, records=records, record_ids=record_ids)
