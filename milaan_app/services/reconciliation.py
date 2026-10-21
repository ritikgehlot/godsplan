from dataclasses import dataclass
from typing import Any

from ..db import get_conn, now_iso


@dataclass
class ReconcileResult:
    invoice_id: str
    result: str
    discrepancy_type: str
    explanation: str
    potential_amount: float
    uncertain: bool
    duplicate: bool


def normalize_invoice_number(value: str) -> str:
    return "".join(ch for ch in value.upper().strip() if ch.isalnum())


def calculate_total_gst(cgst: float, sgst: float, igst: float) -> float:
    return round(float(cgst or 0) + float(sgst or 0) + float(igst or 0), 2)


def detect_duplicate(invoice_number: str, supplier_gstin: str | None) -> bool:
    normalized = normalize_invoice_number(invoice_number)
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT invoice_number, supplier_gstin FROM invoices"
        ).fetchall()
    count = 0
    for row in rows:
        if normalize_invoice_number(row["invoice_number"]) == normalized:
            if (supplier_gstin or "") == (row["supplier_gstin"] or ""):
                count += 1
    return count > 1


def reconcile_invoice(invoice_id: str) -> ReconcileResult:
    with get_conn() as conn:
        inv = conn.execute(
            "SELECT * FROM invoices WHERE invoice_id = ?", (invoice_id,)
        ).fetchone()
        if not inv:
            raise ValueError(f"Invoice {invoice_id} not found")

        gstr_rows = conn.execute(
            "SELECT * FROM gstr2b_records WHERE invoice_number = ?",
            (inv["invoice_number"],),
        ).fetchall()

    duplicate = detect_duplicate(inv["invoice_number"], inv["supplier_gstin"])
    uncertain = bool(inv["uncertain"])

    if duplicate:
        return ReconcileResult(
            invoice_id=invoice_id,
            result="duplicate",
            discrepancy_type="Potential duplicate invoice",
            explanation="Duplicate invoice detected. This invoice will not be double counted.",
            potential_amount=0.0,
            uncertain=uncertain,
            duplicate=True,
        )

    if uncertain:
        return ReconcileResult(
            invoice_id=invoice_id,
            result="uncertain",
            discrepancy_type="Uncertain invoice information",
            explanation="Invoice has uncertain fields and requires user confirmation.",
            potential_amount=float(inv["total_gst"]),
            uncertain=True,
            duplicate=False,
        )

    if not gstr_rows:
        return ReconcileResult(
            invoice_id=invoice_id,
            result="missing",
            discrepancy_type="Missing from demo GSTR-2B",
            explanation=(
                "Invoice not found in the demo GSTR-2B data. "
                "Check the invoice details and supplier reporting before taking action."
            ),
            potential_amount=float(inv["total_gst"]),
            uncertain=False,
            duplicate=False,
        )

    matches = [
        row
        for row in gstr_rows
        if (row["supplier_gstin"] or "") == (inv["supplier_gstin"] or "")
    ]
    if not matches:
        return ReconcileResult(
            invoice_id=invoice_id,
            result="mismatch",
            discrepancy_type="GSTIN mismatch",
            explanation="Invoice number found but supplier GSTIN does not match the imported statement record.",
            potential_amount=float(inv["total_gst"]),
            uncertain=False,
            duplicate=False,
        )

    match = matches[0]
    local_total = calculate_total_gst(inv["cgst"], inv["sgst"], inv["igst"])
    remote_total = float(match["total_gst"])
    if abs(local_total - remote_total) > 0.5:
        return ReconcileResult(
            invoice_id=invoice_id,
            result="mismatch",
            discrepancy_type="GST value mismatch",
            explanation="Invoice and statement values differ and need review.",
            potential_amount=float(inv["total_gst"]),
            uncertain=False,
            duplicate=False,
        )

    return ReconcileResult(
        invoice_id=invoice_id,
        result="matched",
        discrepancy_type="Matched",
        explanation="Invoice matches demo GSTR-2B record on key fields.",
        potential_amount=0.0,
        uncertain=False,
        duplicate=False,
    )


def apply_reconciliation(invoice_id: str) -> dict[str, Any]:
    result = reconcile_invoice(invoice_id)
    with get_conn() as conn:
        now = now_iso()
        status_mapping = {
            "matched": "Matched",
            "missing": "Missing from demo GSTR-2B",
            "uncertain": "Needs Review",
            "duplicate": "Needs Review",
            "mismatch": "Needs Review",
        }
        status = status_mapping[result.result]
        conn.execute(
            """
            UPDATE invoices
            SET status = ?, duplicate_flag = ?, uncertain = ?, updated_at = ?
            WHERE invoice_id = ?
            """,
            (status, int(result.duplicate), int(result.uncertain), now, invoice_id),
        )

        existing_case = conn.execute(
            "SELECT id FROM cases WHERE invoice_id = ? ORDER BY id DESC LIMIT 1", (invoice_id,)
        ).fetchone()
        if status == "Matched":
            if existing_case:
                conn.execute(
                    "UPDATE cases SET status = 'Verified in Demo Data', updated_at = ? WHERE id = ?",
                    (now, existing_case["id"]),
                )
        else:
            next_action = "Review data and decide follow-up"
            case_status = "Needs Review"
            if result.result == "missing":
                next_action = "Prepare supplier follow-up"
                case_status = "Ready for Follow-up"
            if existing_case:
                case_id = existing_case["id"]
                conn.execute(
                    """
                    UPDATE cases
                    SET discrepancy_type = ?, potential_amount = ?, known_facts = ?, uncertainties = ?,
                        recommended_checks = ?, next_action = ?, status = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        result.discrepancy_type,
                        result.potential_amount,
                        result.explanation,
                        "Cause may require independent verification.",
                        "Validate invoice values and supplier filing details.",
                        next_action,
                        case_status,
                        now,
                        case_id,
                    ),
                )
            else:
                cursor = conn.execute(
                    """
                    INSERT INTO cases (
                        invoice_id, discrepancy_type, potential_amount, known_facts, uncertainties,
                        recommended_checks, next_action, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        invoice_id,
                        result.discrepancy_type,
                        result.potential_amount,
                        result.explanation,
                        "Cause may require independent verification.",
                        "Validate invoice values and supplier filing details.",
                        next_action,
                        case_status,
                        now,
                        now,
                    ),
                )
                case_id = cursor.lastrowid

            conn.execute(
                """
                INSERT INTO case_events (case_id, event_type, message, event_meta, created_at)
                VALUES (?, 'reconciliation_updated', ?, ?, ?)
                """,
                (case_id, result.explanation, result.discrepancy_type, now),
            )

    return {
        "invoice_id": result.invoice_id,
        "result": result.result,
        "discrepancy_type": result.discrepancy_type,
        "explanation": result.explanation,
        "potential_amount": result.potential_amount,
        "uncertain": result.uncertain,
        "duplicate": result.duplicate,
        "simulated_ai_note": "Simulated AI output: Recommendation generated from deterministic reconciliation rules.",
    }
