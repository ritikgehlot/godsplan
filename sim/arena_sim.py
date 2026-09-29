"""
SYNTHETIC Battle Arena simulator - for local testing only. Pool, rivals and points are invented;
scores it prints say nothing about the live arena.

Enforces the rules from CLAUDE_CODE_PROMPT.md section 1 in *sim time* (SPEED sim-seconds per
real second): phases, all costs incl. closing double, 50k credits, token-bucket rate limit
(10/s, burst 30, free calls included), 409 in wrong phase, per-team headcount, exclusivity,
same-person detection across ids, rejected offers charged, already_yours, 15-min points
refresh on /ledger, /reason with a deterministic fake model that is sometimes 503.

    python sim/arena_sim.py --port 18800 --seed 1 --speed 150 [--faults 1]

GET /__stats (local runner only) returns ground truth: score per points variant, rate-limit log.
"""
import argparse, heapq, json, math, random, re, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs, unquote

PHASES = [("closed", 60), ("recon", 3600), ("market", 14400), ("closing", 3600)]
PF, CREDITS, TEAM_KEY = 0.05, 50_000, "test"
COST = {"search": 1, "candidate": 2, "batch": 60, "assess": 25, "offer": 10, "release": 5, "market": 2}
ROLES = {
    "Backend Engineer": ["Python", "Go", "PostgreSQL", "Kubernetes", "Kafka", "Docker"],
    "Data Scientist": ["Python", "SQL", "Machine Learning", "Statistics", "PyTorch"],
    "DevOps Engineer": ["Kubernetes", "AWS", "Terraform", "Docker", "CI/CD", "Linux"],
    "Frontend Engineer": ["JavaScript", "TypeScript", "React", "Node.js", "CSS"],
    "Data Engineer": ["Python", "SQL", "Spark", "Airflow", "Kafka", "AWS"],
    "ML Engineer": ["Python", "PyTorch", "TensorFlow", "Machine Learning", "Kubernetes", "Docker"],
}
EXTRA = ["Excel", "Java", "Jira", "Git", "C++", "Scala", "Redis", "GraphQL"]
SPELL = {"Kubernetes": ["k8s", "Kubernetes", "kubernetes", "K8s"], "PostgreSQL": ["Postgres", "postgres", "psql"],
         "Python": ["Python", "python", "Pyhton", "py"], "JavaScript": ["JS", "javascript", "JavaScript"],
         "Node.js": ["Node.js", "NodeJS", "node"], "Machine Learning": ["ML", "Machine Learning"],
         "CI/CD": ["CI/CD", "cicd", "CI-CD"], "TypeScript": ["TS", "typescript"], "PyTorch": ["pytorch", "torch"],
         "TensorFlow": ["tf", "TensorFlow"], "Go": ["Go", "golang"]}
CITIES = ["Bengaluru", "Delhi", "Pune", "Hyderabad", "Chennai", "Mumbai"]
FIRST = ["Rahul", "Asha", "Vikram", "Neha", "Arjun", "Priya", "Kiran", "Sana", "Rohan", "Meera",
         "Aditya", "Isha", "Karan", "Divya", "Nikhil", "Pooja", "Siddharth", "Ananya", "Varun", "Tanvi"]
LAST = ["Sharma", "Iyer", "Khan", "Gupta", "Rao", "Das", "Mehta", "Nair", "Reddy", "Singh",
        "Joshi", "Patel", "Kulkarni", "Bose", "Menon", "Chopra", "Verma", "Pillai", "Saxena", "Ghosh"]
RED = ["could not verify previous employer", "references unreachable", "dates inconsistent across CV",
       "degree certificate could not be verified"]
UNAVAIL = ["accepted another offer last week", "not looking to switch right now", "withdrew from process"]
GREEN = ["strong reference from ex-manager", "verified employment history", "promoted twice, top performer"]
NEUTRAL = ["", "", "", "prefers remote", "open to relocation", "good communicator", "asked about ESOPs"]


class Sim:
    def __init__(self, seed, speed, faults, n_people, latency=None):
        self.rng = random.Random(seed)
        self.speed, self.faults, self.t0, self.latency = speed, faults, time.time(), latency
        self.first_offer_t = None
        self.lock = threading.RLock()
        self.used = 0
        self.holder = {}          # person_id -> (team, cid, rid)
        self.team_holds = {}      # team -> {cid: rid}
        self.release_log = []
        self.req_signings_t = []  # (sim_t, rid) all teams, for price pressure
        self.stats = {"calls": 0, "free_calls": 0, "rate_limited": 0, "http_errors": {}, "charged": {},
                      "offers": {}, "assess": 0, "reason_tokens": 0, "reason_503": 0, "faults": {}}
        self.req_times = []       # sim time of every request (rate-limit proof)
        self.bucket, self.bucket_t, self.bucket_min = 30.0, 0.0, 30.0
        self.reason_in_flight, self.reason_total = 0, 0
        self.points_cache, self.points_as_of = 0.0, -1
        self.make_reqs()
        self.make_pool(n_people)
        self.make_rivals()

    # ------------------------------------------------------------ time
    def now(self):
        return (time.time() - self.t0) * self.speed

    def phase(self, t=None):
        t = self.now() if t is None else t
        for name, dur in PHASES:
            if t < dur:
                return name
            t -= dur
        return "closed"

    def phase_start(self, name):
        t = 0
        for n, dur in PHASES:
            if n == name:
                return t
            t += dur

    # ------------------------------------------------------------ world
    def make_reqs(self):
        r, self.reqs = self.rng, []
        roles = list(ROLES) + ["Backend Engineer", "Data Scientist"]
        for i, role in enumerate(roles):
            skills = r.sample(ROLES[role], r.randint(3, 4))
            min_a = r.choice([55, 60, 65, 70, 75])
            self.reqs.append({"req_id": f"REQ-{i + 1:02d}", "role": role, "skills": skills,
                              "headcount": r.randint(2, 4),
                              "min_assessment": min_a / 10 if i == 3 else min_a,   # one req on a 0-10 scale
                              "max_notice_days": r.choice([30, 45, 60, 90]),
                              "max_expected_ctc": f"{r.choice([18, 22, 28, 35, 45])} LPA",
                              "points": r.choice([60, 80, 100, 120, 150])})
        self.req_by_id = {q["req_id"]: q for q in self.reqs}
        for q in self.reqs:
            q["_min"] = q["min_assessment"] * (10 if q["min_assessment"] <= 10 else 1)
            q["_ctc"] = float(q["max_expected_ctc"].split()[0])

    def fmt_assess(self, x, r):
        k = r.random()
        if k < 0.25: return f"{round(x)}/100"
        if k < 0.45: return f"{x / 10:.1f}/10"
        if k < 0.55: return f"{x / 100:.2f}"
        if k < 0.65: return f"{round(x)}%"
        if k < 0.92: return round(x)
        return None

    def fmt_notice(self, d, r):
        k = r.random()
        if d == 0: return r.choice(["immediate", "Immediate joiner", 0, "none"])
        if k < 0.35: return f"{d} days"
        if k < 0.55 and d % 30 == 0: return f"{d // 30} months" if d > 30 else "1 month"
        if k < 0.65 and d % 7 == 0: return f"{d // 7} weeks"
        if k < 0.85: return d
        if k < 0.92: return "serving notice"
        return None

    def fmt_ctc(self, c, r):
        k = r.random()
        if k < 0.4: return f"{c:g} LPA"
        if k < 0.6: return f"{int(c * 1e5):,}".replace(",", "X").replace("X", ",")
        if k < 0.7: return f"{c * 1e5 / 12 / 1000:.0f}k/month"
        if k < 0.9: return c
        return None

    def make_pool(self, n):
        r = self.rng
        self.people, self.cands, self.by_role = {}, {}, {k: [] for k in ROLES}
        for pid in range(n):
            role = r.choice(list(ROLES))
            fab = r.random() < 0.05
            unavail = not fab and r.random() < 0.03
            junk = not fab and r.random() < 0.015                     # tiny bare 0-100 scores
            true = max(1.0, min(99.0, r.gauss(45, 10) if fab else r.gauss(62, 14)))
            if junk:
                true = float(r.randint(2, 9))
            skills = r.sample(ROLES[role], r.randint(2, len(ROLES[role]))) + r.sample(EXTRA, r.randint(0, 2))
            notice = r.choice([0, 15, 30, 30, 45, 60, 60, 90, 90, 120])
            ctc = round(max(4.0, r.gauss(22, 9)), 1)
            self_a = min(99.0, true + r.uniform(18, 35)) if fab else max(1.0, min(99.0, true + r.gauss(0, 4)))
            if fab and r.random() < 0.55: note = r.choice(RED)
            elif unavail and r.random() < 0.7: note = r.choice(UNAVAIL)
            elif true > 72 and r.random() < 0.3: note = r.choice(GREEN)
            elif r.random() < 0.03: note = "CV dates slightly inconsistent"
            else: note = r.choice(NEUTRAL)
            name = f"{r.choice(FIRST)} {r.choice(LAST)}"
            exp = max(0, int(r.gauss(6, 3)))
            self.people[pid] = {"role": role, "true": true, "skills": skills, "notice": notice, "ctc": ctc,
                                "fab": fab, "unavail": unavail, "self": self_a, "name": name, "exp": exp}
            ids = [pid] + ([pid] if r.random() < 0.05 else [])        # ~5% duplicated under another id
            for j, _ in enumerate(ids):
                cid = f"C{len(self.cands) + 100000}"
                nm = name if j == 0 else r.choice([", ".join(reversed(name.split())), name.lower().replace(" ", "  ")])
                shown = [r.choice(SPELL.get(s, [s])) for s in skills]
                self.cands[cid] = {
                    "pid": pid, "name": nm, "role": role, "city": r.choice(CITIES),
                    "experience": r.choice([f"{exp} yrs", exp, f"{exp * 12} months"]),
                    "skills": ", ".join(shown) if r.random() < 0.7 else shown,
                    "assessment": (true if junk else None) if junk else self.fmt_assess(self_a, r),
                    "notice_period": self.fmt_notice(notice, r), "expected_ctc": self.fmt_ctc(ctc, r),
                    "notes": note, "email": f"{name.lower().replace(' ', '.')}{pid}@mail.test" if r.random() < 0.5 else None}
                if junk:
                    self.cands[cid]["assessment"] = int(true)
                self.by_role[role].append(cid)
        for lst in self.by_role.values():
            r.shuffle(lst)

    # ------------------------------------------------------------ eligibility + hidden points
    def coverage(self, p, q):
        return len(set(p["skills"]) & set(q["skills"])) / len(q["skills"])

    def eligible(self, p, q):
        return (not p["fab"] and not p["unavail"] and p["role"] == q["role"] and p["true"] >= q["_min"]
                and p["notice"] <= q["max_notice_days"] and p["ctc"] <= q["_ctc"] and self.coverage(p, q) >= 0.5)

    def points(self, pid, rid, variant):
        p, q = self.people[pid], self.req_by_id[rid]
        if not self.eligible(p, q):
            return 0.0
        P = q["points"]
        if variant == "margin":
            return P * (0.6 + 0.4 * min(1.0, (p["true"] - q["_min"]) / max(1.0, 100 - q["_min"])))
        if variant == "coverage":
            return P * self.coverage(p, q) * (1 - 0.3 * p["notice"] / q["max_notice_days"])
        return float(P)                                                 # "flat"

    def team_points(self, team, variant):
        return sum(self.points(self.cands[c]["pid"], rid, variant) for c, rid in self.team_holds.get(team, {}).items())

    # ------------------------------------------------------------ rivals (39 bots, in-process)
    def make_rivals(self):
        r, self.events = self.rng, []
        m0 = self.phase_start("market")
        kinds = ["sniper"] * 8 + ["greedy"] * 10 + ["naive"] * 10 + ["smart"] * 6 + ["slow"] * 5
        self.sorted_self = {role: sorted(ids, key=lambda c: -self.people[self.cands[c]["pid"]]["self"])
                            for role, ids in self.by_role.items()}
        self.sorted_true = {role: sorted(ids, key=lambda c: -self.people[self.cands[c]["pid"]]["true"] + r.gauss(0, 5))
                            for role, ids in self.by_role.items()}
        for i, kind in enumerate(kinds):
            team = f"rival{i:02d}-{kind}"
            for q in self.reqs:
                for k in range(q["headcount"]):
                    if kind == "sniper": t = m0 + r.uniform(0.2, 4)
                    elif kind == "greedy": t = m0 + r.uniform(2, 120)
                    elif kind == "naive": t = m0 + r.uniform(30, 900)
                    elif kind == "smart": t = m0 + r.uniform(20, 600)
                    else: t = m0 + r.uniform(600, 14000)
                    heapq.heappush(self.events, (t + k * r.uniform(0.1, 3), i, "sign", team, kind, q["req_id"]))
            for _ in range(2):
                heapq.heappush(self.events, (m0 + r.uniform(1800, 14000), i, "release", team, kind, None))
        threading.Thread(target=self.rival_loop, daemon=True).start()

    def rival_pick(self, kind, q):
        role, r = q["role"], self.rng
        if kind in ("sniper", "greedy"):
            src = self.sorted_self[role]
            ok = lambda p: p["self"] >= q["_min"] and self.coverage(p, q) >= 0.5 and p["notice"] <= q["max_notice_days"]
        elif kind == "naive":
            src = self.by_role[role][: 100]
            ok = lambda p: p["self"] >= q["_min"]
        else:
            src = self.sorted_true[role]
            ok = lambda p: self.eligible(p, q) or r.random() < 0.05
        for c in src:
            p = self.people[self.cands[c]["pid"]]
            if self.cands[c]["pid"] not in self.holder and ok(p):
                return c
        return None

    def rival_loop(self):
        while True:
            t = self.now()
            with self.lock:
                while self.events and self.events[0][0] <= t:
                    _, _, what, team, kind, rid = heapq.heappop(self.events)
                    if self.phase(t) not in ("market", "closing"):
                        continue
                    holds = self.team_holds.setdefault(team, {})
                    if what == "sign":
                        q = self.req_by_id[rid]
                        if sum(1 for v in holds.values() if v == rid) < q["headcount"]:
                            c = self.rival_pick(kind, q)
                            if c:
                                self.sign(team, c, rid, t)
                    elif holds and self.rng.random() < 0.5:
                        c = self.rng.choice(list(holds))
                        self.unsign(team, c)
            time.sleep(0.01)

    def sign(self, team, cid, rid, t):
        self.holder[self.cands[cid]["pid"]] = (team, cid, rid)
        self.team_holds.setdefault(team, {})[cid] = rid
        self.req_signings_t.append((t, rid))

    def unsign(self, team, cid):
        self.team_holds.get(team, {}).pop(cid, None)
        self.holder.pop(self.cands[cid]["pid"], None)
        self.release_log.append((self.now(), team, cid))

    # ------------------------------------------------------------ views
    def summary(self, cid):
        c = self.cands[cid]
        return {k: c[k] for k in ("name", "role", "city", "experience", "skills")} | {"candidate_id": cid}

    def profile(self, cid):
        c = self.cands[cid]
        out = {k: v for k, v in c.items() if k != "pid" and v not in (None, "")}
        out["candidate_id"] = cid
        out["claimed"] = c["pid"] in self.holder
        return out

    def ledger(self):
        t = self.now()
        slot = int(t // 900)
        if slot != self.points_as_of:
            self.points_as_of, self.points_cache = slot, self.team_points(TEAM_KEY, "margin")
        return {"phase": self.phase(t), "credits_used": self.used, "credits_remaining": CREDITS - self.used,
                "signed": len(self.team_holds.get(TEAM_KEY, {})), "points": round(self.points_cache, 2),
                "score": round(self.points_cache - self.used * PF, 2), "points_as_of": slot * 900}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    sim = None

    def log_message(self, *a):
        pass

    def send(self, code, obj, raw=None):
        body = raw if raw is not None else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("X-Credits-Remaining", str(CREDITS - self.sim.used))
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def err(self, code, e):
        s = self.sim.stats["http_errors"]
        s[f"{code}:{e}"] = s.get(f"{code}:{e}", 0) + 1
        self.send(code, {"error": e})

    def charge(self, what, n):
        """True if charged; else sends 429 credits_exhausted (not charged)."""
        s = self.sim
        with s.lock:
            if s.used + n > CREDITS:
                self.err(429, "credits_exhausted")
                return False
            s.used += n
            s.stats["charged"][what] = s.stats["charged"].get(what, 0) + n
            return True

    def rate_ok(self):
        s = self.sim
        with s.lock:
            t = s.now()
            s.bucket = min(30.0, s.bucket + (t - s.bucket_t) * 10.0)
            s.bucket_t = t
            s.req_times.append(t)
            if s.bucket < 1.0:
                s.stats["rate_limited"] += 1
                return False
            s.bucket -= 1.0
            s.bucket_min = min(s.bucket_min, s.bucket)
            return True

    def route(self, method):
        s = self.sim
        u = urlsplit(self.path)
        n = int(self.headers.get("content-length") or 0)
        raw_body = self.rfile.read(n) if n else b""
        if u.path == "/__stats":
            return self.send(200, stats(s))
        if s.latency:                                        # network round trip, in sim seconds
            time.sleep(s.rng.uniform(*s.latency) / s.speed)
        if self.headers.get("X-Arena-Key") != TEAM_KEY:
            return self.err(401, "unknown_key")
        if not self.rate_ok():
            return self.err(429, "rate_limited")
        s.stats["calls"] += 1
        try:
            body = json.loads(raw_body or b"{}")
        except ValueError:
            return self.err(422, "bad_request")
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        path, ph = u.path, s.phase()
        if path == "/ledger":
            s.stats["free_calls"] += 1
            with s.lock:
                return self.send(200, s.ledger())
        if path == "/requisitions":
            s.stats["free_calls"] += 1
            with s.lock:
                mine = s.team_holds.get(TEAM_KEY, {})
                out = []
                for r in s.reqs:
                    f = sum(1 for v in mine.values() if v == r["req_id"])
                    out.append({k: v for k, v in r.items() if not k.startswith("_")} |
                               {"filled": f, "remaining": r["headcount"] - f})
            return self.send(200, out)
        if ph not in ("recon", "market", "closing"):
            return self.err(409, "wrong_phase")
        rng = s.rng
        if s.faults and rng.random() < 0.01 * s.faults:
            s.stats["faults"]["5xx"] = s.stats["faults"].get("5xx", 0) + 1
            return self.err(503, "unavailable")
        malformed = s.faults and rng.random() < 0.003 * s.faults
        res = self.metered(method, path, q, body, ph)
        if res is None:
            return
        code, obj = res
        if malformed and code == 200:
            s.stats["faults"]["malformed"] = s.stats["faults"].get("malformed", 0) + 1
            return self.send(200, None, raw=json.dumps(obj).encode()[: 20])
        self.send(code, obj)

    def metered(self, method, path, q, body, ph):
        s = self.sim
        if path == "/search":
            if not self.charge("search", 1): return None
            size, page = max(1, min(100, int(q.get("size", 100)))), max(0, int(q.get("page", 0)))
            ids = s.by_role.get(q.get("role")) if q.get("role") else [c for l in s.by_role.values() for c in l]
            ids = ids or []
            if q.get("q"):
                needle = q["q"].lower()
                ids = [c for c in ids if needle in str(s.cands[c]["skills"]).lower()]
            chunk = ids[page * size:(page + 1) * size]
            if s.faults and s.rng.random() < 0.002 * s.faults:
                s.stats["faults"]["empty_page"] = s.stats["faults"].get("empty_page", 0) + 1
                chunk = []
            return 200, {"results": [s.summary(c) for c in chunk], "page": page,
                         "has_more": (page + 1) * size < len(ids)}
        if path.startswith("/candidate/"):
            cid = unquote(path.split("/")[-1])
            if cid not in s.cands: return 404, {"error": "unknown_candidate"}
            if not self.charge("candidate", 2): return None
            with s.lock:
                return 200, s.profile(cid)
        if path == "/candidates/batch" and method == "POST":
            ids = body.get("ids") if isinstance(body, dict) else None
            if not isinstance(ids, list) or not 1 <= len(ids) <= 50: return 422, {"error": "bad_request"}
            if not self.charge("batch", 60): return None
            with s.lock:
                return 200, {"profiles": [s.profile(c) for c in ids if c in s.cands]}
        if path.startswith("/assess/"):
            cid = unquote(path.split("/")[-1])
            if cid not in s.cands: return 404, {"error": "unknown_candidate"}
            if not self.charge("assess", 25): return None
            s.stats["assess"] += 1
            p = s.people[s.cands[cid]["pid"]]
            return 200, {"candidate_id": cid, "verified_assessment": round(p["true"]),
                         "reference_check": "failed: employment could not be verified" if p["fab"] else "passed"}
        if path == "/offer" and method == "POST":
            if ph == "recon": return 409, {"error": "wrong_phase"}
            cid, rid = body.get("candidate_id"), body.get("req_id")
            if cid not in s.cands or rid not in s.req_by_id: return 404, {"error": "unknown"}
            if not self.charge("offer", 20 if ph == "closing" else 10): return None
            with s.lock:
                if s.first_offer_t is None:
                    s.first_offer_t = s.now()
                reason = self.offer_reason(cid, rid)
                st = s.stats["offers"]
                st[reason or "accepted"] = st.get(reason or "accepted", 0) + 1
                if reason == "already_yours":
                    return 200, {"accepted": True, "already_yours": True}
                if reason:
                    return 200, {"accepted": False, "reason": reason}
                s.sign(TEAM_KEY, cid, rid, s.now())
                return 200, {"accepted": True, "candidate_id": cid, "req_id": rid}
        if path.startswith("/offer/") and method == "DELETE":
            cid = unquote(path.split("/")[-1])
            with s.lock:
                if cid not in s.team_holds.get(TEAM_KEY, {}): return 404, {"error": "not_held"}
                if not self.charge("release", 5): return None
                s.unsign(TEAM_KEY, cid)
                return 200, {"released": True}
        if path == "/market":
            if not self.charge("market", 2): return None
            with s.lock:
                t = s.now()
                sig, pres = {}, {}
                for tt, rid in s.req_signings_t:
                    sig[rid] = sig.get(rid, 0) + 1
                    if t - tt <= 600: pres[rid] = pres.get(rid, 0) + 1
                scores = {tm: s.team_points(tm, "margin") for tm in s.team_holds}
                mine = scores.get(TEAM_KEY, 0) - s.used * PF
                rank = 1 + sum(1 for tm, v in scores.items() if tm != TEAM_KEY and v > mine)
                return 200, {"signings": sig, "price_pressure": pres, "rank": rank,
                             "leader_score": round(max(scores.values(), default=0), 1)}
        if path == "/reason" and method == "POST":
            return self.reason(body)
        return 404, {"error": "no_route"}

    def offer_reason(self, cid, rid):
        s = self.sim
        pid, mine = s.cands[cid]["pid"], s.team_holds.get(TEAM_KEY, {})
        if mine.get(cid) == rid: return "already_yours"
        h = s.holder.get(pid)
        if h and h[0] == TEAM_KEY: return "same_person_already_signed"
        if h: return "already_signed"
        if s.people[pid]["role"] != s.req_by_id[rid]["role"]: return "role_mismatch"
        if sum(1 for v in mine.values() if v == rid) >= s.req_by_id[rid]["headcount"]: return "requisition_full"
        return None

    def reason(self, body):
        s = self.sim
        prompt = str(body.get("prompt", "")) if isinstance(body, dict) else ""
        mx = int(body.get("max_tokens", 400)) if isinstance(body, dict) else 400
        est = len(prompt) // 4 + mx
        with s.lock:
            if est > 4000 or s.reason_in_flight >= 2 or s.reason_total + est > 450_000 or s.rng.random() < 0.15:
                s.stats["reason_503"] += 1
                return 503, {"error": "model_unavailable"}
            s.reason_in_flight += 1
        try:
            out = {}
            for line in prompt.splitlines():
                m = re.match(r"\s*(C\d+):\s*(.*)", line)
                if m:
                    t = m.group(2).lower()
                    out[m.group(1)] = 0.8 if any(w in t for w in ("verif", "unreach", "inconsisten")) else \
                        0.6 if any(w in t for w in ("accepted another", "not looking", "withdrew")) else 0.1
            text = json.dumps(out)
            tokens = len(prompt) // 4 + len(text) // 4
            if not self.charge("reason", max(1, math.ceil(tokens / 1000))): return None
            with s.lock:
                s.reason_total += tokens
                s.stats["reason_tokens"] += tokens
            return 200, {"completion": text, "tokens": tokens}
        finally:
            with s.lock:
                s.reason_in_flight -= 1

    def do_GET(self): self.route("GET")
    def do_POST(self): self.route("POST")
    def do_DELETE(self): self.route("DELETE")


def stats(s):
    with s.lock:
        mine = s.team_holds.get(TEAM_KEY, {})
        pts = {v: round(s.team_points(TEAM_KEY, v), 2) for v in ("margin", "coverage", "flat")}
        times = sorted(s.req_times)
        j, max_1s = 0, 0
        for i, t in enumerate(times):                      # max requests in any 1-sim-second window
            while times[j] < t - 1.0:
                j += 1
            max_1s = max(max_1s, i - j + 1)
        return {"phase": s.phase(), "sim_t": round(s.now()), "credits_used": s.used,
                "points": pts, "score": {v: round(p - s.used * PF, 2) for v, p in pts.items()},
                "signed": len(mine), "fabricated_signed": sum(s.people[s.cands[c]["pid"]]["fab"] for c in mine),
                "ineligible_signed": sum(not s.eligible(s.people[s.cands[c]["pid"]], s.req_by_id[r]) for c, r in mine.items()),
                "max_requests_any_1s": max_1s, "bucket_min": round(s.bucket_min, 2), "requests": len(times),
                "first_offer_t": s.first_offer_t, "market_open_t": s.phase_start("market"),
                "rejects": {k: v for k, v in s.stats["offers"].items() if k not in ("accepted", "already_yours")},
                **s.stats}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--speed", type=float, default=150)
    ap.add_argument("--faults", type=int, default=1)
    ap.add_argument("--people", type=int, default=95_000)
    ap.add_argument("--start-at", type=float, default=0, help="sim second the clock starts at")
    ap.add_argument("--latency", default="0.2,1.0", help="per-call delay range in sim seconds")
    a = ap.parse_args()
    lat = tuple(float(x) for x in a.latency.split(",")) if a.latency else None
    Handler.sim = Sim(a.seed, a.speed, a.faults, a.people, lat)
    Handler.sim.t0 = time.time() - a.start_at / a.speed   # start the clock after the pool is built
    print(f"SYNTHETIC sim on 127.0.0.1:{a.port} seed={a.seed} speed={a.speed} ids={len(Handler.sim.cands)}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
