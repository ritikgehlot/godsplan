from .db import get_conn, now_iso


SEED_INVOICES = [
    {
        "invoice_id": "INV-101",
        "supplier_name": "Surya Wholesale Traders (Synthetic)",
        "supplier_gstin": "27AAAAA1111A1Z5",
        "invoice_number": "INV-101",
        "invoice_date": "2026-04-05",
        "taxable_value": 10000,
        "cgst": 900,
        "sgst": 900,
        "igst": 0,
        "total_gst": 1800,
        "invoice_total": 11800,
        "status": "Matched",
        "uncertain": 0,
    },
    {
        "invoice_id": "INV-102",
        "supplier_name": "Asha Packaging House (Synthetic)",
        "supplier_gstin": "27BBBBB2222B1Z6",
        "invoice_number": "INV-102",
        "invoice_date": "2026-04-09",
        "taxable_value": 5000,
        "cgst": 450,
        "sgst": 450,
        "igst": 0,
        "total_gst": 900,
        "invoice_total": 5900,
        "status": "Matched",
        "uncertain": 0,
    },
    {
        "invoice_id": "INV-103",
        "supplier_name": "Pragati Distributors (Synthetic)",
        "supplier_gstin": "27CCCCC3333C1Z7",
        "invoice_number": "INV-103",
        "invoice_date": "2026-04-15",
        "taxable_value": 10000,
        "cgst": 900,
        "sgst": 900,
        "igst": 0,
        "total_gst": 1800,
        "invoice_total": 11800,
        "status": "Missing from demo GSTR-2B",
        "uncertain": 0,
    },
    {
        "invoice_id": "INV-104",
        "supplier_name": "Nayi Disha Supplies (Synthetic)",
        "supplier_gstin": "27DDDDD4444D1Z8",
        "invoice_number": "INV-104",
        "invoice_date": "2026-04-19",
        "taxable_value": 10000,
        "cgst": 900,
        "sgst": 900,
        "igst": 0,
        "total_gst": 1800,
        "invoice_total": 11800,
        "status": "Matched",
        "uncertain": 0,
    },
    {
        "invoice_id": "INV-105",
        "supplier_name": "Shakti Electricals (Synthetic)",
        "supplier_gstin": "27EEEEE5555E1Z9",
        "invoice_number": "INV-105",
        "invoice_date": "2026-04-22",
        "taxable_value": 10000,
        "cgst": 900,
        "sgst": 900,
        "igst": 0,
        "total_gst": 1800,
        "invoice_total": 11800,
        "status": "Needs Review",
        "uncertain": 1,
    },
]


SEED_GSTR2B = [
    {
        "supplier_gstin": "27AAAAA1111A1Z5",
        "invoice_number": "INV-101",
        "invoice_date": "2026-04-05",
        "taxable_value": 10000,
        "cgst": 900,
        "sgst": 900,
        "igst": 0,
        "total_gst": 1800,
        "statement_period": "2026-04",
    },
    {
        "supplier_gstin": "27BBBBB2222B1Z6",
        "invoice_number": "INV-102",
        "invoice_date": "2026-04-09",
        "taxable_value": 5000,
        "cgst": 450,
        "sgst": 450,
        "igst": 0,
        "total_gst": 900,
        "statement_period": "2026-04",
    },
    {
        "supplier_gstin": "27DDDDD4444D1Z8",
        "invoice_number": "INV-104",
        "invoice_date": "2026-04-19",
        "taxable_value": 10000,
        "cgst": 900,
        "sgst": 900,
        "igst": 0,
        "total_gst": 1800,
        "statement_period": "2026-04",
    },
]


def seed_demo_data() -> None:
    now = now_iso()
    with get_conn() as conn:
        for item in SEED_INVOICES:
            conn.execute(
                """
                INSERT INTO invoices (
                    invoice_id, supplier_name, supplier_gstin, invoice_number, invoice_date,
                    taxable_value, cgst, sgst, igst, total_gst, invoice_total, status,
                    uncertain, duplicate_flag, source_type, source_ref, extraction_review_required,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'sample', NULL, ?, ?, ?)
                """,
                (
                    item["invoice_id"],
                    item["supplier_name"],
                    item["supplier_gstin"],
                    item["invoice_number"],
                    item["invoice_date"],
                    item["taxable_value"],
                    item["cgst"],
                    item["sgst"],
                    item["igst"],
                    item["total_gst"],
                    item["invoice_total"],
                    item["status"],
                    item["uncertain"],
                    item["uncertain"],
                    now,
                    now,
                ),
            )
        for record in SEED_GSTR2B:
            conn.execute(
                """
                INSERT INTO gstr2b_records (
                    supplier_gstin, invoice_number, invoice_date, taxable_value, cgst, sgst, igst,
                    total_gst, statement_period, source_type, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'sample', ?)
                """,
                (
                    record["supplier_gstin"],
                    record["invoice_number"],
                    record["invoice_date"],
                    record["taxable_value"],
                    record["cgst"],
                    record["sgst"],
                    record["igst"],
                    record["total_gst"],
                    record["statement_period"],
                    now,
                ),
            )

        # Initial discrepancy cases for INV-103 and INV-105
        case_rows = [
            (
                "INV-103",
                "Invoice not found in imported statement",
                1800.0,
                "Invoice exists in purchase records but not in demo GSTR-2B.",
                "Could be reporting delay or normalization issue.",
                "Confirm invoice details and ask supplier to verify filing.",
                "Prepare supplier follow-up",
                "Ready for Follow-up",
            ),
            (
                "INV-105",
                "Uncertain extraction fields",
                1800.0,
                "Invoice exists but extraction confidence is low.",
                "Invoice details require manual confirmation.",
                "Review document fields before reconciliation conclusion.",
                "Manual review required",
                "Needs Review",
            ),
        ]

        for row in case_rows:
            cursor = conn.execute(
                """
                INSERT INTO cases (
                    invoice_id, discrepancy_type, potential_amount, known_facts, uncertainties,
                    recommended_checks, next_action, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (*row, now, now),
            )
            case_id = cursor.lastrowid
            conn.execute(
                """
                INSERT INTO case_events (case_id, event_type, message, event_meta, created_at)
                VALUES (?, 'case_created', ?, NULL, ?)
                """,
                (case_id, f"Case created for {row[0]}", now),
            )
