from typing import Callable

from ..db import get_conn, now_iso
from .case_management import (
    draft_supplier_message,
    get_case,
    update_case_status,
    verify_case_with_later_statement,
)
from .metrics import dashboard_metrics
from .reconciliation import apply_reconciliation, calculate_total_gst, detect_duplicate


class AgentToolError(Exception):
    pass


def _invoice(invoice_id: str):
    with get_conn() as conn:
        inv = conn.execute("SELECT * FROM invoices WHERE invoice_id = ?", (invoice_id,)).fetchone()
    if not inv:
        raise AgentToolError(f"Invoice {invoice_id} not found")
    return dict(inv)


def get_invoice_details(invoice_id: str) -> dict:
    return _invoice(invoice_id)


def list_purchase_invoices() -> dict:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM invoices ORDER BY invoice_id").fetchall()
    return {"items": [dict(r) for r in rows]}


def get_gstr2b_records() -> dict:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM gstr2b_records ORDER BY invoice_number").fetchall()
    return {"items": [dict(r) for r in rows]}


def reconcile_invoice(invoice_id: str) -> dict:
    return apply_reconciliation(invoice_id)


def calculate_tax_difference(invoice_id: str) -> dict:
    inv = _invoice(invoice_id)
    invoice_total = calculate_total_gst(inv["cgst"], inv["sgst"], inv["igst"])
    with get_conn() as conn:
        row = conn.execute(
            "SELECT total_gst FROM gstr2b_records WHERE invoice_number = ? AND supplier_gstin = ?",
            (inv["invoice_number"], inv["supplier_gstin"]),
        ).fetchone()
    statement_total = float(row["total_gst"]) if row else 0.0
    return {
        "invoice_id": invoice_id,
        "invoice_total_gst": invoice_total,
        "statement_total_gst": statement_total,
        "difference": round(invoice_total - statement_total, 2),
    }


def detect_duplicate_invoice(invoice_id: str) -> dict:
    inv = _invoice(invoice_id)
    return {"invoice_id": invoice_id, "duplicate": detect_duplicate(inv["invoice_number"], inv["supplier_gstin"])}


def classify_discrepancy(invoice_id: str) -> dict:
    rec = apply_reconciliation(invoice_id)
    return {
        "invoice_id": invoice_id,
        "classification": rec["discrepancy_type"],
        "known": rec["explanation"],
        "unknown": "Independent verification may still be required.",
    }


def retrieve_case_history(case_id: int) -> dict:
    return get_case(case_id)


def draft_supplier_message_tool(case_id: int) -> dict:
    return {"case_id": case_id, "message": draft_supplier_message(case_id)}


def create_followup_case(invoice_id: str) -> dict:
    with get_conn() as conn:
        exists = conn.execute("SELECT id FROM cases WHERE invoice_id = ? ORDER BY id DESC LIMIT 1", (invoice_id,)).fetchone()
        if exists:
            return get_case(exists["id"])
        inv = conn.execute("SELECT * FROM invoices WHERE invoice_id = ?", (invoice_id,)).fetchone()
        if not inv:
            raise AgentToolError("Invoice not found")

        now = now_iso()
        cursor = conn.execute(
            """
            INSERT INTO cases (
                invoice_id, discrepancy_type, potential_amount, known_facts, uncertainties,
                recommended_checks, next_action, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'Ready for Follow-up', ?, ?)
            """,
            (
                invoice_id,
                "Missing from demo GSTR-2B",
                float(inv["total_gst"]),
                "Invoice missing from imported statement data.",
                "Could be timing or supplier reporting issue.",
                "Request supplier verification and check later statement.",
                "Prepare follow-up",
                now,
                now,
            ),
        )
        case_id = cursor.lastrowid
        return get_case(case_id)


def update_case_status_tool(case_id: int, status: str, note: str | None = None) -> dict:
    return update_case_status(case_id, status, note)


def load_later_statement(records: list[dict], period: str | None = None) -> dict:
    from .case_management import load_later_statement as _load

    count = _load(records, period)
    return {"imported_records": count}


def verify_later_statement_match(case_id: int, statement_period: str | None = None) -> dict:
    return verify_case_with_later_statement(case_id, statement_period)


def calculate_dashboard_metrics() -> dict:
    return dashboard_metrics()


TOOL_REGISTRY: dict[str, Callable] = {
    "get_invoice_details": get_invoice_details,
    "list_purchase_invoices": list_purchase_invoices,
    "get_gstr2b_records": get_gstr2b_records,
    "reconcile_invoice": reconcile_invoice,
    "calculate_tax_difference": calculate_tax_difference,
    "detect_duplicate_invoice": detect_duplicate_invoice,
    "classify_discrepancy": classify_discrepancy,
    "retrieve_case_history": retrieve_case_history,
    "draft_supplier_message": draft_supplier_message_tool,
    "create_followup_case": create_followup_case,
    "update_case_status": update_case_status_tool,
    "load_later_statement": load_later_statement,
    "verify_later_statement_match": verify_later_statement_match,
    "calculate_dashboard_metrics": calculate_dashboard_metrics,
}
