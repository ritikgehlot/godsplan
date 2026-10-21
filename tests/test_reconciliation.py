import unittest

from milaan_app.db import clear_data, init_db
from milaan_app.seed import seed_demo_data
from milaan_app.services.metrics import dashboard_metrics
from milaan_app.services.reconciliation import apply_reconciliation, calculate_total_gst, detect_duplicate, normalize_invoice_number


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        init_db()
        clear_data()
        seed_demo_data()

    def test_initial_dashboard_totals(self):
        totals = dashboard_metrics()["totals"]
        self.assertEqual(totals["total_invoices"], 5)
        self.assertEqual(totals["matched_invoices"], 3)
        self.assertEqual(totals["discrepancies_requiring_review"], 2)
        self.assertEqual(totals["potential_discrepancy_gst"], 3600.0)

    def test_reconcile_missing_invoice(self):
        result = apply_reconciliation("INV-103")
        self.assertEqual(result["result"], "missing")
        self.assertIn("Invoice not found in the demo GSTR-2B data", result["explanation"])

    def test_reconcile_uncertain_invoice(self):
        result = apply_reconciliation("INV-105")
        self.assertEqual(result["result"], "uncertain")

    def test_tax_calculation(self):
        self.assertEqual(calculate_total_gst(900, 900, 0), 1800)

    def test_normalization(self):
        self.assertEqual(normalize_invoice_number(" inv-101 "), "INV101")

    def test_duplicate_detection(self):
        # Seed data does not have duplicate rows
        self.assertFalse(detect_duplicate("INV-101", "27AAAAA1111A1Z5"))


if __name__ == "__main__":
    unittest.main()
