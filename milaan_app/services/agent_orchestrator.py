from ..db import log_activity
from .agent_tools import TOOL_REGISTRY


def run_agent_workflow(invoice_id: str, user_request: str) -> dict:
    steps = []

    def record(step_key: str, status: str, explanation: str, tool_name: str | None = None, tool_result: str | None = None):
        log_activity(step_key, status, explanation, invoice_id=invoice_id, tool_name=tool_name, tool_result=tool_result)
        steps.append(
            {
                "step": step_key,
                "status": status,
                "explanation": explanation,
                "tool": tool_name,
                "tool_result": tool_result,
            }
        )

    record("request_received", "success", f"Request received: {user_request}")

    invoice = TOOL_REGISTRY["get_invoice_details"](invoice_id)
    record("invoice_selected", "success", "Invoice selected for investigation.", "get_invoice_details", f"{invoice['invoice_number']} loaded")

    records = TOOL_REGISTRY["get_gstr2b_records"]()
    record("source_records_retrieved", "success", "Statement records loaded for comparison.", "get_gstr2b_records", f"{len(records['items'])} records")

    rec = TOOL_REGISTRY["reconcile_invoice"](invoice_id)
    record("reconciliation_completed", "success", rec["explanation"], "reconcile_invoice", rec["result"])

    classification = TOOL_REGISTRY["classify_discrepancy"](invoice_id)
    record("discrepancy_identified", "success", classification["classification"], "classify_discrepancy", classification["known"])

    record("evidence_reviewed", "success", classification["unknown"])

    recommendation = "Continue monitoring"
    case_data = None
    if rec["result"] in {"missing", "mismatch", "uncertain", "duplicate"}:
        case_data = TOOL_REGISTRY["create_followup_case"](invoice_id)
        recommendation = case_data["next_action"]

    record("recommendation_prepared", "success", f"Simulated AI output: {recommendation}")

    waiting = rec["result"] == "missing"
    record(
        "waiting_for_approval" if waiting else "workflow_complete",
        "success",
        "Waiting for user approval before any external communication." if waiting else "No follow-up approval required at this stage.",
    )

    return {
        "invoice_id": invoice_id,
        "result": rec,
        "recommendation": recommendation,
        "case": case_data,
        "steps": steps,
        "agent_mode": "deterministic",
        "note": "LLM integration is optional and not active unless configured.",
    }
