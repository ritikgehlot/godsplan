import io
import unittest

from fastapi.testclient import TestClient

from milaan_app.main import app


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def test_health(self):
        r = self.client.get("/api/health")
        self.assertEqual(r.status_code, 200)

    def test_dashboard(self):
        self.client.post("/api/reset-demo")
        r = self.client.get("/api/dashboard")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["totals"]["total_invoices"], 5)

    def test_reconcile_all(self):
        self.client.post("/api/reset-demo")
        r = self.client.post("/api/reconcile-all")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()["results"]), 5)

    def test_upload_validation(self):
        r = self.client.post(
            "/api/invoices/upload",
            files={"file": ("bad.exe", io.BytesIO(b"123"), "application/octet-stream")},
        )
        self.assertEqual(r.status_code, 400)

    def test_agent_workflow(self):
        self.client.post("/api/reset-demo")
        r = self.client.post(
            "/api/agent/run",
            json={"invoice_id": "INV-103", "user_request": "Please review discrepancy"},
        )
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertGreater(len(data["steps"]), 5)


if __name__ == "__main__":
    unittest.main()
