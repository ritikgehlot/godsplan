from collections import Counter

from ..db import get_conn


def dashboard_metrics() -> dict:
    with get_conn() as conn:
        invoices = conn.execute("SELECT * FROM invoices ORDER BY invoice_id").fetchall()
        cases = conn.execute("SELECT * FROM cases").fetchall()
        activities = conn.execute(
            "SELECT * FROM activity_log ORDER BY id DESC LIMIT 10"
        ).fetchall()

    total = len(invoices)
    matched = sum(1 for i in invoices if i["status"] == "Matched")
    discrepancies = sum(
        1
        for i in invoices
        if i["status"] in {"Missing from demo GSTR-2B", "Needs Review"}
    )
    potential_gst = sum(
        float(i["total_gst"])
        for i in invoices
        if i["status"] in {"Missing from demo GSTR-2B", "Needs Review"} and not i["duplicate_flag"]
    )
    open_cases = sum(1 for c in cases if c["status"] not in {"Verified in Demo Data", "Closed Without Verification"})
    awaiting = sum(1 for c in cases if c["status"] == "Awaiting Verification")
    verified_demo = sum(1 for c in cases if c["status"] == "Verified in Demo Data")

    invoice_status_dist = dict(Counter(i["status"] for i in invoices))
    case_status_dist = dict(Counter(c["status"] for c in cases))

    return {
        "totals": {
            "total_invoices": total,
            "matched_invoices": matched,
            "discrepancies_requiring_review": discrepancies,
            "potential_discrepancy_gst": round(potential_gst, 2),
            "open_cases": open_cases,
            "cases_awaiting_verification": awaiting,
            "verified_demo_cases": verified_demo,
        },
        "invoice_status_distribution": invoice_status_dist,
        "case_status_distribution": case_status_dist,
        "recent_agent_activity": [dict(row) for row in activities],
        "disclaimer": "Illustrative demo metrics only. Potential discrepancies are not guaranteed recoveries.",
    }
