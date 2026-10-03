"""MailProvider (section 9): only messages where the user is a recipient,
and only those from the contact email of the supplier the attention item
names, or that mention the inbound PO's id. Everything else in the
mailbox (a newsletter, an unrelated vendor, an internal note, a message
mentioning the same PO but addressed to someone else) is noise and must
not reach the planner.
"""

from __future__ import annotations

import json

from harness.context.base import ContextSlice, ProviderContext
from harness.detection.base import AttentionItem
from harness.world.users import User


class MailProvider:
    source = "mail"

    def fetch(self, ctx: ProviderContext, user: User, item: AttentionItem) -> ContextSlice:
        if "mail:read" not in user.scopes:
            return ContextSlice(source=self.source, records=[], record_ids=[])

        conn = ctx.conn
        relevant_po_id = item.facts.get("inbound_po_id")
        relevant_supplier_id = item.facts.get("supplier_id")
        relevant_contact = None
        if relevant_supplier_id:
            row = conn.execute(
                "SELECT contact_email FROM erp_suppliers WHERE supplier_id = ?", (relevant_supplier_id,)
            ).fetchone()
            relevant_contact = row[0] if row else None

        records: list[dict] = []
        record_ids: list[str] = []
        for row in conn.execute(
            "SELECT message_id, sender, recipients, sent_at, subject, body FROM mail_messages"
        ).fetchall():
            if user.email not in json.loads(row["recipients"]):
                continue
            mentions_po = bool(relevant_po_id) and relevant_po_id not in ("none",) and (
                relevant_po_id in row["subject"] or relevant_po_id in row["body"]
            )
            from_relevant_supplier = bool(relevant_contact) and row["sender"] == relevant_contact
            if not (mentions_po or from_relevant_supplier):
                continue
            records.append({
                "message_id": row["message_id"], "sender": row["sender"], "sent_at": row["sent_at"],
                "subject": row["subject"], "body": row["body"],
            })
            record_ids.append(row["message_id"])

        return ContextSlice(source=self.source, records=records, record_ids=record_ids)
