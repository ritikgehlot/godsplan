import csv
import io
import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import ALLOWED_UPLOAD_EXTENSIONS, DISCLAIMER, MAX_UPLOAD_BYTES, UPLOAD_DIR
from .db import clear_data, get_conn, init_db, log_activity, now_iso
from .schemas import (
    AgentRequest,
    CaseStatusUpdate,
    FollowupApprovalRequest,
    GSTR2BImportRecord,
    LaterStatementVerifyRequest,
    ManualInvoiceCreate,
)
from .seed import seed_demo_data
from .services.agent_orchestrator import run_agent_workflow
from .services.case_management import (
    approve_followup,
    draft_supplier_message,
    get_case,
    list_cases,
    load_later_statement,
    update_case_status,
    verify_case_with_later_statement,
)
from .services.metrics import dashboard_metrics
from .services.reconciliation import apply_reconciliation, calculate_total_gst, normalize_invoice_number

app = FastAPI(title="MILAAN", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def startup() -> None:
    init_db()
    with get_conn() as conn:
        count = conn.execute("SELECT COUNT(*) c FROM invoices").fetchone()["c"]
    if count == 0:
        seed_demo_data()


STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "service": "MILAAN"}


@app.post("/api/reset-demo")
def reset_demo() -> dict:
    clear_data()
    seed_demo_data()
    log_activity("demo_reset", "success", "Demo dataset reset and re-seeded.")
    return {"ok": True, "message": "Demo reset complete."}


@app.get("/api/dashboard")
def get_dashboard() -> dict:
    metrics = dashboard_metrics()
    metrics["platform_disclaimer"] = DISCLAIMER
    return metrics


@app.get("/api/invoices")
def list_invoices(status: str | None = None, supplier: str | None = None, date_from: str | None = None, date_to: str | None = None) -> dict:
    clauses = []
    params: list[Any] = []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if supplier:
        clauses.append("supplier_name LIKE ?")
        params.append(f"%{supplier}%")
    if date_from:
        clauses.append("invoice_date >= ?")
        params.append(date_from)
    if date_to:
        clauses.append("invoice_date <= ?")
        params.append(date_to)
    query = "SELECT * FROM invoices"
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY invoice_date, invoice_number"

    with get_conn() as conn:
        rows = conn.execute(query, params).fetchall()
    return {"items": [dict(r) for r in rows], "count": len(rows)}


@app.get("/api/invoices/{invoice_id}")
def get_invoice(invoice_id: str) -> dict:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM invoices WHERE invoice_id = ?", (invoice_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Invoice not found")
    return dict(row)


@app.post("/api/invoices/manual")
def create_manual_invoice(payload: ManualInvoiceCreate) -> dict:
    total_gst = calculate_total_gst(payload.cgst, payload.sgst, payload.igst)
    expected_total = round(payload.taxable_value + total_gst, 2)
    if payload.invoice_total is not None and abs(payload.invoice_total - expected_total) > 1:
        raise HTTPException(400, "Invoice total does not match taxable value + GST components")

    normalized_number = normalize_invoice_number(payload.invoice_number)
    invoice_id = f"MAN-{normalized_number}"
    now = now_iso()

    with get_conn() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO invoices (
                invoice_id, supplier_name, supplier_gstin, invoice_number, invoice_date,
                taxable_value, cgst, sgst, igst, total_gst, invoice_total, status,
                uncertain, duplicate_flag, source_type, source_ref, extraction_review_required,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Needs Review', ?, 0, 'manual', NULL, ?, ?, ?)
            """,
            (
                invoice_id,
                payload.supplier_name,
                payload.supplier_gstin,
                payload.invoice_number,
                payload.invoice_date,
                payload.taxable_value,
                payload.cgst,
                payload.sgst,
                payload.igst,
                total_gst,
                payload.invoice_total or expected_total,
                int(payload.uncertain),
                int(payload.uncertain),
                now,
                now,
            ),
        )

    log_activity("manual_invoice_created", "success", f"Manual invoice {invoice_id} captured.", invoice_id=invoice_id)
    return {"invoice_id": invoice_id, "expected_total": expected_total, "total_gst": total_gst}


@app.post("/api/invoices/upload")
async def upload_invoice(file: UploadFile = File(...)) -> dict:
    suffix = Path(file.filename).suffix.lower()
    if suffix not in ALLOWED_UPLOAD_EXTENSIONS:
        raise HTTPException(400, "Unsupported file type. Upload PDF/JPG/PNG only.")

    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(400, "File too large. Max size is 10 MB.")

    safe_name = Path(file.filename).name
    stored_name = f"{now_iso().replace(':', '-')}_{safe_name}"
    path = UPLOAD_DIR / stored_name
    path.write_bytes(content)

    with get_conn() as conn:
        conn.execute(
            "INSERT INTO uploads (filename, stored_path, content_type, file_size, created_at) VALUES (?, ?, ?, ?, ?)",
            (safe_name, str(path), file.content_type, len(content), now_iso()),
        )

    log_activity(
        "invoice_uploaded",
        "success",
        "Invoice file uploaded for review. OCR is not automatic in this demo.",
        tool_name="upload_invoice",
        tool_result=file.filename,
    )
    return {
        "message": "Upload successful. Manual review required for extraction in this demo.",
        "filename": safe_name,
        "size": len(content),
    }


def _parse_csv_rows(raw: str) -> list[dict]:
    reader = csv.DictReader(io.StringIO(raw))
    rows = []
    for row in reader:
        if not row.get("invoice_number"):
            continue
        parsed = GSTR2BImportRecord(
            supplier_gstin=row.get("supplier_gstin") or None,
            invoice_number=row["invoice_number"],
            invoice_date=row.get("invoice_date") or None,
            taxable_value=float(row["taxable_value"]) if row.get("taxable_value") else None,
            cgst=float(row.get("cgst") or 0),
            sgst=float(row.get("sgst") or 0),
            igst=float(row.get("igst") or 0),
            total_gst=float(row.get("total_gst") or 0),
            statement_period=row.get("statement_period") or None,
        )
        rows.append(parsed.model_dump())
    return rows


@app.post("/api/gstr2b/import")
async def import_gstr2b(file: UploadFile = File(...)) -> dict:
    if not file.filename.lower().endswith(".csv"):
        raise HTTPException(400, "Only CSV imports are supported.")
    raw = (await file.read()).decode("utf-8", errors="ignore")
    rows = _parse_csv_rows(raw)
    if not rows:
        raise HTTPException(400, "No valid records found in CSV")
    with get_conn() as conn:
        conn.execute("DELETE FROM gstr2b_records")
        now = now_iso()
        for r in rows:
            conn.execute(
                """
                INSERT INTO gstr2b_records (
                    supplier_gstin, invoice_number, invoice_date, taxable_value,
                    cgst, sgst, igst, total_gst, statement_period, source_type, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'import', ?)
                """,
                (
                    r["supplier_gstin"],
                    r["invoice_number"],
                    r["invoice_date"],
                    r["taxable_value"],
                    r["cgst"],
                    r["sgst"],
                    r["igst"],
                    r["total_gst"],
                    r["statement_period"],
                    now,
                ),
            )
    log_activity("gstr2b_imported", "success", f"Imported {len(rows)} statement records.", tool_name="import_gstr2b", tool_result=str(len(rows)))
    return {"imported": len(rows)}


@app.post("/api/reconcile/{invoice_id}")
def reconcile_single(invoice_id: str) -> dict:
    try:
        result = apply_reconciliation(invoice_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc

    log_activity(
        "reconciliation_completed",
        "success",
        result["explanation"],
        invoice_id=invoice_id,
        tool_name="reconcile_invoice",
        tool_result=result["result"],
    )
    return result


@app.post("/api/reconcile-all")
def reconcile_all() -> dict:
    with get_conn() as conn:
        invoices = conn.execute("SELECT invoice_id FROM invoices ORDER BY invoice_id").fetchall()
    results = []
    for row in invoices:
        results.append(apply_reconciliation(row["invoice_id"]))
    log_activity("reconcile_all", "success", f"Reconciled {len(results)} invoices.")
    return {"results": results}


@app.get("/api/cases")
def get_cases() -> dict:
    return {"items": list_cases()}


@app.get("/api/cases/{case_id}")
def read_case(case_id: int) -> dict:
    try:
        return get_case(case_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/api/cases/{case_id}/status")
def change_case_status(case_id: int, payload: CaseStatusUpdate) -> dict:
    try:
        case = update_case_status(case_id, payload.status, payload.note)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    log_activity("case_status_updated", "success", f"Case {case_id} moved to {payload.status}", case_id=case_id, tool_name="update_case_status")
    return case


@app.post("/api/cases/{case_id}/draft-followup")
def draft_followup(case_id: int) -> dict:
    try:
        msg = draft_supplier_message(case_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    log_activity("recommendation_prepared", "success", "Supplier follow-up drafted.", case_id=case_id, tool_name="draft_supplier_message")
    return {"case_id": case_id, "message": msg}


@app.post("/api/cases/{case_id}/approve-followup")
def approve_case_followup(case_id: int, payload: FollowupApprovalRequest) -> dict:
    try:
        case = approve_followup(case_id, payload.message)
        case = update_case_status(case_id, "Awaiting Verification", "Moved to awaiting verification after follow-up simulation")
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    log_activity(
        "followup_simulated",
        "success",
        "Demo simulation only — no real message was sent.",
        case_id=case_id,
        tool_name="approve_followup",
    )
    return case


@app.post("/api/later-statement/import")
async def import_later_statement(file: UploadFile = File(...), statement_period: str | None = None) -> dict:
    if not file.filename.lower().endswith(".csv"):
        raise HTTPException(400, "Only CSV imports are supported.")
    raw = (await file.read()).decode("utf-8", errors="ignore")
    rows = _parse_csv_rows(raw)
    if not rows:
        raise HTTPException(400, "No valid records found in CSV")
    count = load_later_statement(rows, statement_period)
    log_activity("later_statement_checked", "success", f"Loaded {count} later statement records.", tool_name="load_later_statement", tool_result=str(count))
    return {"imported": count}


@app.post("/api/cases/{case_id}/verify-later")
def verify_case(case_id: int, payload: LaterStatementVerifyRequest) -> dict:
    try:
        case = verify_case_with_later_statement(case_id, payload.statement_period)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    log_activity("case_outcome_updated", "success", f"Case {case_id} verification updated to {case['status']}", case_id=case_id, tool_name="verify_later_statement_match")
    return case


@app.post("/api/agent/run")
def run_agent(payload: AgentRequest) -> dict:
    try:
        result = run_agent_workflow(payload.invoice_id, payload.user_request)
    except Exception as exc:  # noqa: BLE001
        log_activity("agent_run", "failure", f"Agent workflow failed: {exc}", invoice_id=payload.invoice_id)
        raise HTTPException(400, str(exc)) from exc
    return result


@app.get("/api/activity")
def list_activity(limit: int = 30) -> dict:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM activity_log ORDER BY id DESC LIMIT ?", (max(1, min(limit, 200)),)
        ).fetchall()
    return {"items": [dict(r) for r in rows]}


@app.get("/api/disclaimer")
def disclaimer() -> dict:
    return {
        "text": DISCLAIMER,
        "integration_honesty": {
            "ocr": "Simulated/manual review",
            "llm": "Deterministic rules by default",
            "gst_portal": "Not connected",
            "whatsapp": "Not connected; follow-up is simulated",
        },
    }


@app.get("/api/tool-specs")
def tool_specs() -> dict:
    return {
        "tools": [
            {"name": "get_invoice_details", "input": {"invoice_id": "string"}, "output": {"invoice": "object"}},
            {"name": "list_purchase_invoices", "input": {}, "output": {"items": "invoice[]"}},
            {"name": "get_gstr2b_records", "input": {}, "output": {"items": "gstr2b[]"}},
            {"name": "reconcile_invoice", "input": {"invoice_id": "string"}, "output": {"result": "object"}},
            {"name": "calculate_tax_difference", "input": {"invoice_id": "string"}, "output": {"difference": "number"}},
            {"name": "detect_duplicate_invoice", "input": {"invoice_id": "string"}, "output": {"duplicate": "boolean"}},
            {"name": "classify_discrepancy", "input": {"invoice_id": "string"}, "output": {"classification": "string"}},
            {"name": "retrieve_case_history", "input": {"case_id": "number"}, "output": {"case": "object"}},
            {"name": "draft_supplier_message", "input": {"case_id": "number"}, "output": {"message": "string"}},
            {"name": "create_followup_case", "input": {"invoice_id": "string"}, "output": {"case": "object"}},
            {"name": "update_case_status", "input": {"case_id": "number", "status": "string"}, "output": {"case": "object"}},
            {"name": "load_later_statement", "input": {"records": "array"}, "output": {"imported_records": "number"}},
            {"name": "verify_later_statement_match", "input": {"case_id": "number"}, "output": {"case": "object"}},
            {"name": "calculate_dashboard_metrics", "input": {}, "output": {"metrics": "object"}},
        ]
    }
