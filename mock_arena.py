"""
Local mock of the Battle Arena API - for CRASH-TESTING ONLY.

The pool is synthetic and the scoring is invented, so the score it prints means nothing.
It exists to exercise every code path: phases, 409s, 429 rate limits, empty searches,
rejected offers (other "teams" grab names at market open), duplicates, batch, /reason 503.

    python mock_arena.py            # serves on :8000, phases: 20s closed... see PHASES
    ARENA_URL=http://localhost:8000 ARENA_KEY=test python agent.py
"""
import json, random, re, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs

random.seed(7)
PHASES = [("closed", 3), ("recon", 25), ("market", 40), ("closing", 10), ("closed_final", 9999)]
T0 = time.time()
CREDITS = {"left": 50_000, "used": 0}
LOCK = threading.Lock()
ROLES = ["Backend Engineer", "Data Scientist", "DevOps Engineer"]
SKILLS = {"Backend Engineer": ["Python", "Go", "PostgreSQL", "Kubernetes"],
          "Data Scientist": ["Python", "PyTorch", "SQL", "Statistics"],
          "DevOps Engineer": ["Kubernetes", "AWS", "Terraform", "Docker"]}
SPELL = {"Kubernetes": ["k8s", "Kubernetes", "kubernetes"], "PostgreSQL": ["postgres", "Postgres"],
         "Python": ["python", "Pyhton", "py"], "PyTorch": ["pytorch", "torch"]}
REQS = [{"req_id": f"R{i}", "role": r, "skills": SKILLS[r], "headcount": 3, "min_assessment": 70,
         "max_notice_days": 60, "max_expected_ctc": "30 LPA", "points": 100} for i, r in enumerate(ROLES)]
POOL, CLAIMED, OURS = {}, {}, {}


def make_pool(n=20_000):
    firsts, lasts = ["Rahul", "Asha", "Vikram", "Neha", "Arjun", "Priya", "Kiran", "Sana"], \
        ["Sharma", "Iyer", "Khan", "Gupta", "Rao", "Das", "Mehta", "Nair"]
    for i in range(n):
        role = random.choice(ROLES)
        sk = random.sample(SKILLS[role], random.randint(1, 4)) + random.sample(["Excel", "Java", "React"], 1)
        sk = [random.choice(SPELL.get(s, [s])) for s in sk]
        true = random.gauss(65, 12)
        fake = random.random() < 0.05
        name = f"{random.choice(firsts)} {random.choice(lasts)} {i % 997}"
        POOL[f"C{i}"] = {"candidate_id": f"C{i}", "name": name, "role": role, "city": "Delhi",
                         "experience": f"{random.randint(1, 12)} yrs", "skills": ", ".join(sk),
                         "assessment": random.choice([f"{true + (20 if fake else 0):.0f}/100", f"{true / 10:.1f}/10", None]),
                         "notice_period": random.choice(["immediate", "30 days", "2 months", "90 days"]),
                         "expected_ctc": random.choice(["18 LPA", "25,00,000", "35 LPA"]),
                         "notes": "could not verify previous employer" if fake else random.choice(["", "strong reference"]),
                         "_true": true, "_fake": fake}
    for i in range(0, 200, 2):                                   # duplicates under another id
        d = dict(POOL[f"C{i}"]); d["candidate_id"] = f"D{i}"; d["name"] = ", ".join(reversed(d["name"].split(" ", 1)))
        POOL[f"D{i}"] = d


def phase():
    t = time.time() - T0
    for name, dur in PHASES:
        if t < dur:
            return "closed" if name == "closed_final" else name
        t -= dur
    return "closed"


def person(cid):
    return re.sub(r"[^a-z0-9]", "", "".join(sorted(POOL[cid]["name"].lower().replace(",", "").split())))


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("X-Credits-Remaining", str(CREDITS["left"]))
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def charge(self, n):
        with LOCK:
            if CREDITS["left"] < n:
                self.send(429, {"error": "credits_exhausted"}); return False
            CREDITS["left"] -= n; CREDITS["used"] += n; return True

    def public(self, cid, full):
        p = {k: v for k, v in POOL[cid].items() if not k.startswith("_")}
        if not full:
            p = {k: p[k] for k in ("candidate_id", "name", "role", "city", "experience", "skills")}
        else:
            p["claimed"] = person(cid) in CLAIMED
        return p

    def route(self, method):
        u = urlsplit(self.path); q = {k: v[0] for k, v in parse_qs(u.query).items()}
        n = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}") if n else {}
        ph = phase(); path = u.path
        if random.random() < 0.01:
            return self.send(429, {"error": "rate_limited"})
        if path == "/ledger":
            pts = sum(0 if POOL[c]["_fake"] or POOL[c]["_true"] < 70 else 100 for c in OURS)
            return self.send(200, {"phase": ph, "credits_used": CREDITS["used"], "signed": len(OURS),
                                   "points": pts, "score": pts - CREDITS["used"] * 0.05})
        if path == "/requisitions":
            return self.send(200, [dict(r, filled=sum(1 for v in OURS.values() if v == r["req_id"])) for r in REQS])
        if ph not in ("recon", "market", "closing"):
            return self.send(409, {"error": "wrong_phase"})
        if path == "/search":
            if not self.charge(1): return
            ids = [c for c in POOL if not q.get("role") or POOL[c]["role"] == q["role"]]
            page, size = int(q.get("page", 0)), min(100, int(q.get("size", 100)))
            return self.send(200, {"results": [self.public(c, False) for c in ids[page * size:(page + 1) * size]]})
        if path.startswith("/candidate/"):
            if not self.charge(2): return
            cid = path.split("/")[-1]
            return self.send(200, self.public(cid, True)) if cid in POOL else self.send(404, {"error": "unknown"})
        if path == "/candidates/batch":
            if not self.charge(60): return
            return self.send(200, {"profiles": [self.public(c, True) for c in body["ids"][:50] if c in POOL]})
        if path.startswith("/assess/"):
            if not self.charge(25): return
            c = POOL[path.split("/")[-1]]
            return self.send(200, {"verified_assessment": round(c["_true"]), "reference_check": "failed" if c["_fake"] else "passed"})
        if path == "/market":
            if not self.charge(2): return
            return self.send(200, {"rank": 7, "leader_score": 900, "price_pressure": {"R0": 5}})
        if path == "/reason":
            return self.send(503, {"error": "model_disabled"})
        if path == "/offer" and method == "POST":
            if ph == "recon": return self.send(409, {"error": "wrong_phase"})
            if not self.charge(20 if ph == "closing" else 10): return
            cid, rid = body["candidate_id"], body["req_id"]
            with LOCK:
                if person(cid) in CLAIMED:
                    same = CLAIMED[person(cid)] == "us"
                    return self.send(200, {"accepted": False, "reason": "same_person_already_signed" if same else "already_signed"})
                if sum(1 for v in OURS.values() if v == rid) >= 3:
                    return self.send(200, {"accepted": False, "reason": "requisition_full"})
                CLAIMED[person(cid)] = "us"; OURS[cid] = rid
            return self.send(200, {"accepted": True})
        if path.startswith("/offer/") and method == "DELETE":
            if not self.charge(5): return
            cid = path.split("/")[-1]
            with LOCK:
                OURS.pop(cid, None); CLAIMED.pop(person(cid), None)
            return self.send(200, {"released": True})
        self.send(404, {"error": "no_route"})

    def do_GET(self): self.route("GET")
    def do_POST(self): self.route("POST")
    def do_DELETE(self): self.route("DELETE")


def rivals():
    """At market open, 'other teams' grab the obviously best names."""
    while phase() != "market":
        time.sleep(0.2)
    best = sorted(POOL, key=lambda c: -POOL[c]["_true"])[:60]
    for c in best:
        with LOCK:
            CLAIMED.setdefault(person(c), "rival")


if __name__ == "__main__":
    make_pool()
    threading.Thread(target=rivals, daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", 8000), H).serve_forever()
