# MILAAN — AI-Powered GST Invoice Reconciliation and Follow-up Agent

**Tagline:** Recover What You Deserve.

MILAAN is a working full-stack demo application for SARCathon 2026 Round 2. It helps small businesses reconcile purchase invoices against GSTR-2B records, investigate discrepancies, draft supplier follow-ups with explicit approval, track cases, and verify outcomes against later statement data.

> **Disclaimer:** Potential discrepancies require independent verification. MILAAN does not guarantee ITC eligibility or recovery.

---

## What this repository contains

- FastAPI backend with SQLite persistence
- Web UI (single-page workflow app) with dashboard, invoice workspace, agent panel, case management, and impact view
- Deterministic reconciliation engine and tool-based agent orchestrator
- Seed/demo data with required 5 invoices and derived metrics
- CSV import for GSTR-2B and later-statement verification
- Simulated supplier follow-up approval workflow
- Unit/integration tests

---

## Tech stack

- Python 3.10+
- FastAPI + Uvicorn
- SQLite (local persistent demo DB)
- Vanilla HTML/CSS/JS frontend (served by FastAPI)

---

## Project structure

```
.
├── milaan_app/
│   ├── main.py                  # API + app bootstrap
│   ├── db.py                    # SQLite schema + helpers
│   ├── seed.py                  # Demo seed data
│   ├── schemas.py               # Input schemas and status enums
│   ├── config.py                # Paths, limits, disclaimers
│   ├── services/
│   │   ├── reconciliation.py    # Deterministic matching + discrepancy logic
│   │   ├── case_management.py   # Case lifecycle + follow-up + verification
│   │   ├── metrics.py           # Dashboard and impact metrics
│   │   ├── agent_tools.py       # Tool interface for orchestration
│   │   └── agent_orchestrator.py# Multi-step deterministic agent workflow
│   └── static/
│       ├── index.html
│       ├── styles.css
│       └── app.js
├── tests/
│   ├── test_reconciliation.py
│   ├── test_cases.py
│   └── test_api.py
├── sample_data/
│   ├── gstr2b_sample.csv
│   └── later_statement_sample.csv
├── .env.example
└── requirements.txt
```

---

## Setup

```bash
cd /home/runner/work/godsplan/godsplan
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Optional environment variables:

- `MILAAN_DB_PATH` (default `./data/milaan.db`)
- `MILAAN_UPLOAD_DIR` (default `./uploads`)

---

## Run

```bash
uvicorn milaan_app.main:app --reload --host 0.0.0.0 --port 8000
```

Open: `http://localhost:8000`

---

## Core workflows implemented

1. **Dashboard metrics (data-derived):**
   - Total invoices
   - Matched invoices
   - Discrepancies requiring review
   - Potential discrepancy GST amount
   - Open/awaiting/verified case counts

2. **Invoice workspace:**
   - Upload invoice file (PDF/JPG/PNG) with type/size validation
   - Manual invoice entry with GST arithmetic checks
   - Invoice filtering + per-invoice reconciliation

3. **Reconciliation engine (deterministic):**
   - Match by invoice number + GSTIN + GST values
   - Detect missing, uncertain, mismatch, duplicate scenarios
   - Missing message includes required cautionary text

4. **Agent workspace:**
   - Structured tool-driven workflow (not free-form hidden reasoning)
   - Activity panel with step statuses and tool outcomes

5. **Supplier follow-up + approval:**
   - Draft professional follow-up
   - User review/edit/approve required
   - Records simulated send only
   - Explicitly states: **"Demo simulation only — no real message was sent."**

6. **Case lifecycle + verification:**
   - Controlled transitions across states:
     - Needs Review
     - Ready for Follow-up
     - Follow-up Approved
     - Follow-up Simulated
     - Awaiting Verification
     - Verified in Demo Data
     - Closed Without Verification
   - Later statement import + verification gate before marked verified

---

## Seed data

Includes required synthetic demo records:

- INV-101 (GST ₹1,800) Matched
- INV-102 (GST ₹900) Matched
- INV-103 (GST ₹1,800) Missing from demo GSTR-2B
- INV-104 (GST ₹1,800) Matched
- INV-105 (GST ₹1,800) Needs Review

Initial calculated totals:

- Total invoices: 5
- Matched: 3
- Requiring review: 2
- Potential discrepancy GST: ₹3,600

---

## Tests

Run:

```bash
python -m unittest discover -s tests -v
```

Coverage focus:

- Tax arithmetic
- Normalization
- Reconciliation outcomes
- Missing/uncertain handling
- Case transition rules
- Follow-up approval workflow
- Later-statement verification
- API behavior and workflow endpoints

---

## Implemented vs simulated integrations

### Implemented
- Local SQLite persistence
- CSV imports for statement data
- Deterministic reconciliation and case workflows
- Simulated delivery adapter recording follow-up events

### Simulated / not connected
- Live GST portal integration
- Real WhatsApp/email dispatch
- Real OCR extraction
- Live LLM reasoning (deterministic fallback is active by default)

---

## 3-minute demo script

See: `DEMO_SCRIPT.md`

