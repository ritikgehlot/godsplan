import sqlite3
from contextlib import contextmanager
from datetime import datetime

from .config import DATA_DIR, DB_PATH, UPLOAD_DIR


def now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


def init_db() -> None:
    ensure_dirs()
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript(
            """
            PRAGMA foreign_keys=ON;

            CREATE TABLE IF NOT EXISTS invoices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                invoice_id TEXT UNIQUE NOT NULL,
                supplier_name TEXT NOT NULL,
                supplier_gstin TEXT,
                invoice_number TEXT NOT NULL,
                invoice_date TEXT NOT NULL,
                taxable_value REAL NOT NULL,
                cgst REAL NOT NULL DEFAULT 0,
                sgst REAL NOT NULL DEFAULT 0,
                igst REAL NOT NULL DEFAULT 0,
                total_gst REAL NOT NULL,
                invoice_total REAL,
                status TEXT NOT NULL,
                uncertain INTEGER NOT NULL DEFAULT 0,
                duplicate_flag INTEGER NOT NULL DEFAULT 0,
                source_type TEXT NOT NULL DEFAULT 'sample',
                source_ref TEXT,
                extraction_review_required INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS gstr2b_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                supplier_gstin TEXT,
                invoice_number TEXT NOT NULL,
                invoice_date TEXT,
                taxable_value REAL,
                cgst REAL NOT NULL DEFAULT 0,
                sgst REAL NOT NULL DEFAULT 0,
                igst REAL NOT NULL DEFAULT 0,
                total_gst REAL NOT NULL,
                statement_period TEXT,
                source_type TEXT NOT NULL DEFAULT 'sample',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS later_statement_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                supplier_gstin TEXT,
                invoice_number TEXT NOT NULL,
                invoice_date TEXT,
                taxable_value REAL,
                total_gst REAL,
                statement_period TEXT,
                source_type TEXT NOT NULL DEFAULT 'sample',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS cases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                invoice_id TEXT NOT NULL,
                discrepancy_type TEXT NOT NULL,
                potential_amount REAL NOT NULL,
                known_facts TEXT NOT NULL,
                uncertainties TEXT NOT NULL,
                recommended_checks TEXT NOT NULL,
                next_action TEXT NOT NULL,
                status TEXT NOT NULL,
                followup_message TEXT,
                followup_approved INTEGER NOT NULL DEFAULT 0,
                followup_simulated INTEGER NOT NULL DEFAULT 0,
                verified_in_demo_data INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(invoice_id) REFERENCES invoices(invoice_id)
            );

            CREATE TABLE IF NOT EXISTS case_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                message TEXT NOT NULL,
                event_meta TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(case_id) REFERENCES cases(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS activity_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                step_key TEXT NOT NULL,
                status TEXT NOT NULL,
                invoice_id TEXT,
                case_id INTEGER,
                explanation TEXT NOT NULL,
                tool_name TEXT,
                tool_result TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS followup_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id INTEGER NOT NULL,
                message TEXT NOT NULL,
                approved INTEGER NOT NULL,
                simulated_sent INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(case_id) REFERENCES cases(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS uploads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filename TEXT NOT NULL,
                stored_path TEXT NOT NULL,
                content_type TEXT,
                file_size INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )


@contextmanager
def get_conn():
    ensure_dirs()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def clear_data() -> None:
    with get_conn() as conn:
        for table in [
            "case_events",
            "followup_history",
            "activity_log",
            "cases",
            "later_statement_records",
            "gstr2b_records",
            "uploads",
            "invoices",
        ]:
            conn.execute(f"DELETE FROM {table}")


def log_activity(step_key: str, status: str, explanation: str, invoice_id: str | None = None, case_id: int | None = None, tool_name: str | None = None, tool_result: str | None = None) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO activity_log (step_key, status, invoice_id, case_id, explanation, tool_name, tool_result, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (step_key, status, invoice_id, case_id, explanation, tool_name, tool_result, now_iso()),
        )
