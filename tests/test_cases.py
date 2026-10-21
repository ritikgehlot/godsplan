import unittest

from milaan_app.db import clear_data, init_db
from milaan_app.seed import seed_demo_data
from milaan_app.services.case_management import (
    approve_followup,
    draft_supplier_message,
    list_cases,
    update_case_status,
    verify_case_with_later_statement,
)
from milaan_app.services.case_management import load_later_statement


class CaseWorkflowTests(unittest.TestCase):
    def setUp(self):
        init_db()
        clear_data()
        seed_demo_data()

    def _case_for_invoice(self, invoice_id: str):
        for c in list_cases():
            if c["invoice_id"] == invoice_id:
                return c
        raise AssertionError("case not found")

    def test_followup_requires_flow(self):
        case = self._case_for_invoice("INV-103")
        msg = draft_supplier_message(case["id"])
        self.assertIn("INV-103", msg)

        approved = approve_followup(case["id"], msg)
        self.assertEqual(approved["status"], "Follow-up Simulated")

    def test_case_transition_validation(self):
        case = self._case_for_invoice("INV-103")
        with self.assertRaises(ValueError):
            update_case_status(case["id"], "Verified in Demo Data")

    def test_later_statement_verification(self):
        case = self._case_for_invoice("INV-103")
        msg = draft_supplier_message(case["id"])
        approve_followup(case["id"], msg)
        update_case_status(case["id"], "Awaiting Verification")

        load_later_statement(
            [
                {
                    "supplier_gstin": "27CCCCC3333C1Z7",
                    "invoice_number": "INV-103",
                    "invoice_date": "2026-05-10",
                    "taxable_value": 10000,
                    "total_gst": 1800,
                    "statement_period": "2026-05",
                }
            ],
            "2026-05",
        )

        verified = verify_case_with_later_statement(case["id"], "2026-05")
        self.assertEqual(verified["status"], "Verified in Demo Data")


if __name__ == "__main__":
    unittest.main()
