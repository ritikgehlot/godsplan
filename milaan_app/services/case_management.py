from ..db import get_conn, now_iso
from ..schemas import CASE_STATUSES

ALLOWED_TRANSITIONS = {
    "Needs Review": {"Ready for Follow-up", "Closed Without Verification"},
    "Ready for Follow-up": {"Follow-up Approved", "Closed Without Verification"},
    "Follow-up Approved": {"Follow-up Simulated", "Closed Without Verification"},
    "Follow-up Simulated": {"Awaiting Verification", "Closed Without Verification"},
    "Awaiting Verification": {"Verified in Demo Data", "Closed Without Verification"},
    "Verified in Demo Data": set(),
    "Closed Without Verification": set(),
}


def list_cases() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM cases ORDER BY updated_at DESC"
        ).fetchall()
    return [dict(row) for row in rows]


def get_case(case_id: int) -> dict:
    with get_conn() as conn:
        case = conn.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone()
        if not case:
            raise ValueError("Case not found")
        events = conn.execute(
            "SELECT * FROM case_events WHERE case_id = ? ORDER BY id", (case_id,)
        ).fetchall()
        followups = conn.execute(
            "SELECT * FROM followup_history WHERE case_id = ? ORDER BY id DESC", (case_id,)
        ).fetchall()
    data = dict(case)
    data["events"] = [dict(e) for e in events]
    data["followups"] = [dict(f) for f in followups]
    return data


def update_case_status(case_id: int, new_status: str, note: str | None = None) -> dict:
    if new_status not in CASE_STATUSES:
        raise ValueError("Invalid status")

    with get_conn() as conn:
        case = conn.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone()
        if not case:
            raise ValueError("Case not found")
        current = case["status"]
        if new_status != current and new_status not in ALLOWED_TRANSITIONS[current]:
            raise ValueError(f"Invalid transition from {current} to {new_status}")

        now = now_iso()
        conn.execute(
            "UPDATE cases SET status = ?, updated_at = ? WHERE id = ?",
            (new_status, now, case_id),
        )
        message = note or f"Case status updated from {current} to {new_status}"
        conn.execute(
            """
            INSERT INTO case_events (case_id, event_type, message, event_meta, created_at)
            VALUES (?, 'status_changed', ?, ?, ?)
            """,
            (case_id, message, f"{current}->{new_status}", now),
        )

    return get_case(case_id)


def draft_supplier_message(case_id: int) -> str:
    with get_conn() as conn:
        case = conn.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone()
        if not case:
            raise ValueError("Case not found")
        inv = conn.execute(
            "SELECT * FROM invoices WHERE invoice_id = ?", (case["invoice_id"],)
        ).fetchone()
        if not inv:
            raise ValueError("Invoice not found")

    return (
        f"Dear {inv['supplier_name']},\n\n"
        f"We are reconciling our GST purchase records and observed a discrepancy for invoice "
        f"{inv['invoice_number']} dated {inv['invoice_date']} (Taxable Value ₹{inv['taxable_value']:.2f}, "
        f"GST ₹{inv['total_gst']:.2f}).\n\n"
        "Could you please verify the reporting status in your return and confirm if any correction is required?\n\n"
        "This is a verification request from a demo workflow and requires independent review.\n\n"
        "Regards,\nMILAAN User"
    )


def approve_followup(case_id: int, message: str) -> dict:
    with get_conn() as conn:
        case = conn.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone()
        if not case:
            raise ValueError("Case not found")
        if case["status"] not in {"Ready for Follow-up", "Follow-up Approved", "Needs Review"}:
            raise ValueError("Follow-up approval not allowed in current status")

        now = now_iso()
        conn.execute(
            """
            UPDATE cases
            SET followup_message = ?, followup_approved = 1, followup_simulated = 1,
                status = 'Follow-up Simulated', updated_at = ?
            WHERE id = ?
            """,
            (message, now, case_id),
        )
        conn.execute(
            """
            INSERT INTO followup_history (case_id, message, approved, simulated_sent, created_at)
            VALUES (?, ?, 1, 1, ?)
            """,
            (case_id, message, now),
        )
        conn.execute(
            """
            INSERT INTO case_events (case_id, event_type, message, event_meta, created_at)
            VALUES (?, 'followup_simulated', ?, 'demo_only', ?)
            """,
            (case_id, "Demo simulation only — no real message was sent.", now),
        )

    return get_case(case_id)


def load_later_statement(records: list[dict], period: str | None = None) -> int:
    with get_conn() as conn:
        now = now_iso()
        for row in records:
            conn.execute(
                """
                INSERT INTO later_statement_records (
                    supplier_gstin, invoice_number, invoice_date, taxable_value,
                    total_gst, statement_period, source_type, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'import', ?)
                """,
                (
                    row.get("supplier_gstin"),
                    row.get("invoice_number"),
                    row.get("invoice_date"),
                    row.get("taxable_value"),
                    row.get("total_gst"),
                    period or row.get("statement_period"),
                    now,
                ),
            )
        return len(records)


def verify_case_with_later_statement(case_id: int, statement_period: str | None = None) -> dict:
    with get_conn() as conn:
        case = conn.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone()
        if not case:
            raise ValueError("Case not found")
        inv = conn.execute("SELECT * FROM invoices WHERE invoice_id = ?", (case["invoice_id"],)).fetchone()
        if not inv:
            raise ValueError("Invoice not found")

        query = "SELECT * FROM later_statement_records WHERE invoice_number = ?"
        params = [inv["invoice_number"]]
        if statement_period:
            query += " AND statement_period = ?"
            params.append(statement_period)
        rows = conn.execute(query, params).fetchall()

        matched = any((r["supplier_gstin"] or "") == (inv["supplier_gstin"] or "") for r in rows)
        now = now_iso()
        if matched:
            conn.execute(
                "UPDATE cases SET status = 'Verified in Demo Data', verified_in_demo_data = 1, updated_at = ? WHERE id = ?",
                (now, case_id),
            )
            conn.execute(
                "UPDATE invoices SET status = 'Matched', updated_at = ? WHERE invoice_id = ?",
                (now, inv["invoice_id"]),
            )
            msg = "Case verified in demo later statement data."
            event_type = "verified_demo"
        else:
            conn.execute(
                "UPDATE cases SET status = 'Awaiting Verification', updated_at = ? WHERE id = ?",
                (now, case_id),
            )
            msg = "No matching later statement record found; case remains awaiting verification."
            event_type = "verification_pending"

        conn.execute(
            "INSERT INTO case_events (case_id, event_type, message, event_meta, created_at) VALUES (?, ?, ?, ?, ?)",
            (case_id, event_type, msg, statement_period, now),
        )

    return get_case(case_id)
