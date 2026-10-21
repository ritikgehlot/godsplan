const state = {
  currentView: "dashboard",
  selectedCaseId: null,
};

const byId = (id) => document.getElementById(id);

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (!res.ok) {
    const t = await res.text();
    throw new Error(t || `Request failed: ${res.status}`);
  }
  const ct = res.headers.get("content-type") || "";
  return ct.includes("application/json") ? res.json() : res.text();
}

function rupee(num) {
  return `₹${Number(num || 0).toLocaleString("en-IN", { maximumFractionDigits: 2 })}`;
}

function statusBadge(status) {
  const cls = status === "Matched" ? "matched" : status.includes("Missing") ? "missing" : "review";
  return `<span class="badge ${cls}">${status}</span>`;
}

function activateView(view) {
  state.currentView = view;
  document.querySelectorAll(".view").forEach((el) => el.classList.remove("active"));
  byId(view).classList.add("active");
  byId("viewTitle").textContent = {
    dashboard: "Dashboard",
    invoices: "Invoice Workspace",
    agent: "Agent Workspace",
    cases: "Case Management",
    impact: "Impact & Evaluation",
  }[view];
}

async function loadDashboard() {
  const data = await api("/api/dashboard");
  byId("globalDisclaimer").textContent = data.platform_disclaimer || "";
  const t = data.totals;
  byId("dashboard").innerHTML = `
    <div class="card">
      <h3>Overview</h3>
      <div class="metric-grid">
        <div class="metric"><label>Total Invoices</label><strong>${t.total_invoices}</strong></div>
        <div class="metric"><label>Matched</label><strong>${t.matched_invoices}</strong></div>
        <div class="metric"><label>Discrepancies</label><strong>${t.discrepancies_requiring_review}</strong></div>
        <div class="metric"><label>Potential Discrepancy GST</label><strong>${rupee(t.potential_discrepancy_gst)}</strong></div>
        <div class="metric"><label>Open Cases</label><strong>${t.open_cases}</strong></div>
        <div class="metric"><label>Awaiting Verification</label><strong>${t.cases_awaiting_verification}</strong></div>
        <div class="metric"><label>Verified Demo Cases</label><strong>${t.verified_demo_cases}</strong></div>
      </div>
      <p class="subtle">All figures are synthetic/illustrative demo data.</p>
    </div>
    <div class="card">
      <h3>Status Distribution</h3>
      <p><strong>Invoice:</strong> ${Object.entries(data.invoice_status_distribution).map(([k,v]) => `${k}: ${v}`).join(" | ")}</p>
      <p><strong>Case:</strong> ${Object.entries(data.case_status_distribution).map(([k,v]) => `${k}: ${v}`).join(" | ")}</p>
    </div>
    <div class="card"><h3>Recent Agent Activity</h3>
      ${(data.recent_agent_activity || []).map(a => `<div class="step"><div><strong>${a.step_key}</strong> · ${a.status}</div><div>${a.explanation}</div><small>${a.created_at}</small></div>`).join("") || "<p>No activity yet.</p>"}
    </div>
  `;

  byId("impactPanel").innerHTML = `
    <h3>Impact Summary</h3>
    <p>Invoices checked: <strong>${t.total_invoices}</strong></p>
    <p>Matching invoices: <strong>${t.matched_invoices}</strong></p>
    <p>Discrepancies requiring review: <strong>${t.discrepancies_requiring_review}</strong></p>
    <p>Potential GST linked to discrepancies: <strong>${rupee(t.potential_discrepancy_gst)}</strong></p>
    <p class="subtle">${data.disclaimer}</p>
  `;
}

async function loadInvoices() {
  const status = byId("filterStatus").value;
  const supplier = byId("filterSupplier").value;
  const params = new URLSearchParams();
  if (status) params.append("status", status);
  if (supplier) params.append("supplier", supplier);

  const data = await api(`/api/invoices?${params.toString()}`);
  byId("invoiceTable").innerHTML = data.items
    .map(
      (inv) => `
      <tr>
        <td>${inv.invoice_number}</td>
        <td>${inv.supplier_name}</td>
        <td>${inv.invoice_date}</td>
        <td>${rupee(inv.total_gst)}</td>
        <td>${statusBadge(inv.status)} ${inv.uncertain ? "<span class='badge review'>Uncertain</span>" : ""}${inv.duplicate_flag ? " <span class='badge review'>Duplicate</span>" : ""}</td>
        <td><button data-reconcile="${inv.invoice_id}">Reconcile</button></td>
      </tr>`
    )
    .join("");

  const options = data.items.map((x) => `<option value="${x.invoice_id}">${x.invoice_id} · ${x.invoice_number}</option>`).join("");
  byId("agentInvoice").innerHTML = options;

  document.querySelectorAll("[data-reconcile]").forEach((btn) => {
    btn.onclick = async () => {
      try {
        const id = btn.getAttribute("data-reconcile");
        const result = await api(`/api/reconcile/${id}`, { method: "POST" });
        alert(`Result: ${result.result}\n${result.explanation}`);
        await refreshAll();
      } catch (e) {
        alert(e.message);
      }
    };
  });
}

async function loadCases() {
  const data = await api("/api/cases");
  byId("caseTable").innerHTML = data.items
    .map(
      (c) => `
      <tr>
        <td>#${c.id}</td>
        <td>${c.invoice_id}</td>
        <td>${statusBadge(c.status)}</td>
        <td>${c.discrepancy_type}</td>
        <td>${rupee(c.potential_amount)}</td>
        <td>
          <button data-open-case="${c.id}">Open</button>
        </td>
      </tr>`
    )
    .join("");

  document.querySelectorAll("[data-open-case]").forEach((btn) => {
    btn.onclick = () => openCase(Number(btn.getAttribute("data-open-case")));
  });
}

async function openCase(caseId) {
  state.selectedCaseId = caseId;
  const c = await api(`/api/cases/${caseId}`);
  const transitions = ["Needs Review","Ready for Follow-up","Follow-up Approved","Follow-up Simulated","Awaiting Verification","Verified in Demo Data","Closed Without Verification"];

  byId("caseDetail").innerHTML = `
    <p><strong>Invoice:</strong> ${c.invoice_id}</p>
    <p><strong>Status:</strong> ${statusBadge(c.status)}</p>
    <p><strong>Known:</strong> ${c.known_facts}</p>
    <p><strong>Uncertain:</strong> ${c.uncertainties}</p>
    <p><strong>Recommended checks:</strong> ${c.recommended_checks}</p>
    <div class="row">
      <button id="draftMsgBtn">Review Message</button>
      <button id="approveMsgBtn">Approve Demo Follow-up</button>
      <button id="verifyLaterBtn">Verify with Later Statement</button>
      <select id="caseStatusSelect">${transitions.map(s=>`<option ${s===c.status?"selected":""}>${s}</option>`).join("")}</select>
      <button id="moveCaseBtn">Update Status</button>
    </div>
    <div id="followupComposer"></div>
    <h4>Event History</h4>
    ${(c.events || []).map(e => `<div class='step'><strong>${e.event_type}</strong><div>${e.message}</div><small>${e.created_at}</small></div>`).join("")}
  `;

  byId("draftMsgBtn").onclick = async () => {
    const d = await api(`/api/cases/${caseId}/draft-followup`, { method: "POST" });
    byId("followupComposer").innerHTML = `
      <p class='warning'>Review required before simulated send.</p>
      <textarea id='followupText' rows='8' style='width:100%'>${d.message}</textarea>
    `;
  };

  byId("approveMsgBtn").onclick = async () => {
    const text = byId("followupText")?.value;
    if (!text) return alert("Please draft/review the message first.");
    await api(`/api/cases/${caseId}/approve-followup`, { method: "POST", body: JSON.stringify({ message: text }) });
    alert("Demo simulation only — no real message was sent.");
    await refreshAll();
    await openCase(caseId);
  };

  byId("moveCaseBtn").onclick = async () => {
    const status = byId("caseStatusSelect").value;
    try {
      await api(`/api/cases/${caseId}/status`, { method: "POST", body: JSON.stringify({ status }) });
      await refreshAll();
      await openCase(caseId);
    } catch (e) {
      alert(e.message);
    }
  };

  byId("verifyLaterBtn").onclick = async () => {
    await api(`/api/cases/${caseId}/verify-later`, { method: "POST", body: JSON.stringify({ statement_period: byId("laterPeriod").value || null }) });
    await refreshAll();
    await openCase(caseId);
  };
}

async function runAgent(event) {
  event.preventDefault();
  const payload = {
    invoice_id: byId("agentInvoice").value,
    user_request: byId("agentRequest").value,
  };

  const result = await api("/api/agent/run", { method: "POST", body: JSON.stringify(payload) });
  byId("agentSteps").innerHTML = result.steps
    .map(
      (s) => `<div class="step"><strong>${s.step.replaceAll("_", " ")}</strong>
        <div>${s.explanation}</div>
        <small>Status: ${s.status}${s.tool ? ` · Tool: ${s.tool}` : ""}${s.tool_result ? ` · Result: ${s.tool_result}` : ""}</small></div>`
    )
    .join("");

  await refreshAll();
}

async function uploadInvoice(event) {
  event.preventDefault();
  const file = byId("invoiceFile").files[0];
  if (!file) return;
  const fd = new FormData();
  fd.append("file", file);
  const res = await fetch("/api/invoices/upload", { method: "POST", body: fd });
  const text = await res.text();
  byId("uploadResult").textContent = text;
}

async function saveManualInvoice(event) {
  event.preventDefault();
  const form = new FormData(event.target);
  const payload = Object.fromEntries(form.entries());
  payload.taxable_value = Number(payload.taxable_value);
  payload.cgst = Number(payload.cgst);
  payload.sgst = Number(payload.sgst);
  payload.igst = Number(payload.igst);
  payload.invoice_total = payload.invoice_total ? Number(payload.invoice_total) : null;
  payload.uncertain = Boolean(form.get("uncertain"));

  try {
    const res = await api("/api/invoices/manual", { method: "POST", body: JSON.stringify(payload) });
    byId("manualResult").textContent = `Saved: ${res.invoice_id}`;
    event.target.reset();
    await refreshAll();
  } catch (e) {
    byId("manualResult").textContent = e.message;
  }
}

async function importLater(event) {
  event.preventDefault();
  const file = byId("laterFile").files[0];
  if (!file) return;
  const fd = new FormData();
  fd.append("file", file);
  const period = byId("laterPeriod").value;
  const url = period ? `/api/later-statement/import?statement_period=${encodeURIComponent(period)}` : "/api/later-statement/import";
  const res = await fetch(url, { method: "POST", body: fd });
  const text = await res.text();
  byId("laterResult").textContent = text;
  await refreshAll();
}

async function refreshAll() {
  await Promise.all([loadDashboard(), loadInvoices(), loadCases()]);
}

async function init() {
  document.querySelectorAll("[data-view]").forEach((btn) => {
    btn.onclick = () => activateView(btn.getAttribute("data-view"));
  });

  byId("resetDemo").onclick = async () => {
    await api("/api/reset-demo", { method: "POST" });
    await refreshAll();
    activateView("dashboard");
  };

  byId("applyInvoiceFilters").onclick = loadInvoices;
  byId("agentForm").onsubmit = runAgent;
  byId("uploadForm").onsubmit = uploadInvoice;
  byId("manualForm").onsubmit = saveManualInvoice;
  byId("laterForm").onsubmit = importLater;

  await refreshAll();
}

init().catch((e) => {
  alert(`Initialization failed: ${e.message}`);
});
