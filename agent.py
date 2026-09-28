"""
god'splan - Battle Arena agent (Innov8 4.0 finale).

One rule drives every purchase:  buy an action only if the points it is expected to add
exceed  credits_it_costs x PENALTY_FACTOR.  PENALTY_FACTOR is an env var, set at kick-off.

Phases
  recon   (offers locked)  read wide and cheap: role-filtered search pages at 1 credit/100,
          keep a top-K per requisition, batch-buy full profiles (1.2 cr each vs 2), score
          fraud risk from the text, send only grey-zone notes to the LLM (batched), and
          /assess only where risk x slot value > 25 x PF. Output: a ranked offer queue.
  market  fire the queue the second offers open (one worker per requisition, throttled
          below the rate limit), fall back down the queue on rejection, then run a slow
          loop: fill empty slots, verify held-but-risky signings, upgrade weak holds.
  closing offers cost 20: only fill empty slots whose value clears the doubled price.
  closed  print the ledger and exit.

State is checkpointed to disk so a crash restart does not re-buy what we already know.
Every paid decision is written to decisions.jsonl with the reason, for the judges.
"""
import difflib, heapq, json, math, os, re, threading, time, traceback
from arena_client import Arena, Exhausted, WrongPhase


# ============================================================ knobs (env overrides)
def _env(name, default, cast=float):
    value = os.environ.get(name)
    return cast(value) if value not in (None, "") else default


PENALTY_FACTOR = _env("PENALTY_FACTOR", 0.05)      # announced at kick-off. SET THIS.
DEFAULT_REQ_POINTS = _env("DEFAULT_REQ_POINTS", 100.0)  # used only if /requisitions has no points field
PROFILE_MULT = _env("PROFILE_MULT", 6.0)          # full profiles bought per open slot in recon
ASSESS_MULT = _env("ASSESS_MULT", 2.0)            # max pre-market assessments per open slot
TOPK_PER_REQ = _env("TOPK_PER_REQ", 400, int)     # summaries kept per requisition
SEARCH_PATIENCE = _env("SEARCH_PATIENCE", 15, int)  # pages with no top-K improvement -> stop
MIN_PAGES_PER_ROLE = _env("MIN_PAGES_PER_ROLE", 100, int)  # read at least this deep (1 cr/page)
MAX_PAGES_PER_ROLE = _env("MAX_PAGES_PER_ROLE", 400, int)
P_OK_MIN = _env("P_OK_MIN", 0.6)                  # never sign below this estimated P(eligible)
UPGRADE_MIN_GAIN = _env("UPGRADE_MIN_GAIN", 10.0)  # points; release+re-sign only above this
USE_LLM = _env("USE_LLM", 1, int)
LLM_TOKEN_BUDGET = _env("LLM_TOKEN_BUDGET", 120_000, int)  # hard cap is 450K
MARKET_POLL_S = _env("MARKET_POLL_S", 300, int)   # /market costs 2; only while slots are open
LOOP_SLEEP_S = _env("LOOP_SLEEP_S", 5, int)
RPS = _env("RPS", 8.0)                            # stay under 10/s (free calls count too)
STATE_PATH = os.environ.get("STATE_PATH", "agent_state.json")
DECISIONS_PATH = os.environ.get("DECISIONS_PATH", "decisions.jsonl")

COST = {"search": 1, "profile": 2, "batch": 60, "assess": 25, "offer": 10,
        "offer_closing": 20, "release": 5, "market": 2}


def log(*msg):
    print(time.strftime("%H:%M:%S"), *msg, flush=True)


# ============================================================ throttled client
class ThrottledArena(Arena):
    """Spaces every call (free ones too) so the team never hits 429 rate_limited."""

    def __init__(self, rps=RPS, **kw):
        super().__init__(**kw)
        self._gap, self._next, self._tlock = 1.0 / rps, 0.0, threading.Lock()
        self.samples_logged = set()

    def _call(self, path, *a, **kw):
        with self._tlock:
            now = time.monotonic()
            wait = self._next - now
            self._next = max(now, self._next) + self._gap
        if wait > 0:
            time.sleep(wait)
        result = super()._call(path, *a, **kw)
        endpoint = path.split("?")[0].split("/")[1] if "/" in path else path
        if endpoint not in self.samples_logged:           # show real response shapes once
            self.samples_logged.add(endpoint)
            log(f"SAMPLE {endpoint}: {json.dumps(result, default=str)[:700]}")
        return result


# ============================================================ parsing messy fields
NUM = re.compile(r"\d+(?:\.\d+)?")
WORD_NUMS = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
             "a month": "1 month", "half": "0.5"}


def field(d, *names, default=None):
    """First present, non-empty value among several possible key names."""
    for n in names:
        if isinstance(d, dict) and d.get(n) not in (None, "", []):
            return d[n]
    return default


def safe_num(v, default):
    """Number from int/float/'100 pts'/'3 people'; default if there is none."""
    if isinstance(v, bool) or v is None:
        return default
    if isinstance(v, (int, float)):
        return float(v)
    m = NUM.search(str(v).replace(",", ""))
    return float(m.group()) if m else default


def parse_score100(v):
    """'71/100'->71, '7.1/10'->71, '0.71'->71, '71%'->71, 71->71, junk->None."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        x = float(v)
    else:
        s = str(v).lower().replace(",", "")
        m = re.search(r"(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)", s)
        if m:
            den = float(m.group(2))
            return 100.0 * float(m.group(1)) / den if den else None
        m = NUM.search(s)
        if not m:
            return None
        x = float(m.group())
        if "%" in s:
            return x
    if x <= 1.0:
        return x * 100
    if x <= 10.0:
        return x * 10
    return x


def parse_days(v):
    """Notice period in days: 'immediate'->0, '2 months'->60, '3 weeks'->21, 45->45."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).lower()
    for w, n in WORD_NUMS.items():
        s = s.replace(w, n)
    m = NUM.search(s)
    if any(k in s for k in ("immediate", "available now", "can join now")) and not m:
        return 0.0
    if not m:
        return None
    x = float(m.group())
    if "month" in s:
        x *= 30
    elif "week" in s:
        x *= 7
    return x


def parse_lakh(v):
    """CTC in lakh/yr: '12 LPA'->12, '12,00,000'->12, '1.2 Cr'->120, '80k/month'->9.6."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        x = float(v)
        return x / 1e5 if x >= 1000 else x
    s = str(v).lower().replace(",", "").replace("₹", "").replace("inr", "").replace("rs.", "")
    m = NUM.search(s)
    if not m:
        return None
    x = float(m.group())
    monthly = any(k in s for k in ("month", "/mo", "pm", "p.m"))
    if re.search(r"cr|crore", s):
        x *= 100
    elif re.search(r"lpa|lakh|lac|\d\s*l\b", s):
        pass
    elif re.search(r"\d\s*k\b", s):
        x = x * 1000 / 1e5
    elif x >= 1000:
        x = x / 1e5
    return x * 12 if monthly else x


def parse_years(v):
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).lower()
    m = NUM.search(s)
    if not m:
        return None
    x = float(m.group())
    return x / 12 if "month" in s and "year" not in s else x


# ============================================================ skills
ALIASES = {
    "js": "javascript", "ecmascript": "javascript", "es6": "javascript",
    "ts": "typescript", "py": "python", "python3": "python",
    "k8s": "kubernetes", "kube": "kubernetes", "postgres": "postgresql", "psql": "postgresql",
    "pg": "postgresql", "mongo": "mongodb", "golang": "go", "reactjs": "react",
    "node": "nodejs", "nodejs": "nodejs", "ml": "machinelearning", "dl": "deeplearning",
    "tf": "tensorflow", "torch": "pytorch", "sklearn": "scikitlearn", "scikit": "scikitlearn",
    "amazonwebservices": "aws", "googlecloud": "gcp", "googlecloudplatform": "gcp",
    "msazure": "azure", "nlp": "naturallanguageprocessing", "computervision": "computervision",
    "springboot": "spring", "net": "dotnet", "aspnet": "dotnet", "c#": "csharp",
    "cpp": "c++", "cicd": "cicd", "ci/cd": "cicd", "msexcel": "excel", "powerbi": "powerbi",
    "vuejs": "vue", "angularjs": "angular", "nextjs": "next", "llm": "llms", "genai": "llms",
}
VOCAB = set()          # every skill any requisition asks for; filled after /requisitions
_canon_cache = {}


def skill_key(raw):
    k = re.sub(r"[\s\.\-_]", "", str(raw).lower().strip())
    return ALIASES.get(k, k)


def skill_set(raw):
    if not raw:
        return set()
    items = raw if isinstance(raw, list) else re.split(r"[,;|/]", str(raw))
    return {skill_key(s) for s in items if str(s).strip()}


def canon(k):
    """Map a candidate skill onto the requisition vocabulary, tolerating typos ('pyhton')."""
    if k in VOCAB or not VOCAB:
        return k
    if k not in _canon_cache:
        m = difflib.get_close_matches(k, list(VOCAB), n=1, cutoff=0.8)
        _canon_cache[k] = m[0] if m else k
    return _canon_cache[k]


def coverage(cand_skills, wanted):
    """Fraction of wanted skills the candidate has."""
    if not wanted:
        return 1.0
    have = {canon(s) for s in cand_skills}
    return len(wanted & have) / len(wanted)


def name_key(summary):
    """Duplicate-person key: sorted name tokens + role. 'Sharma, Rahul' == 'rahul  sharma'."""
    name = str(field(summary, "name", "full_name", default=""))
    toks = sorted(t for t in re.split(r"[^a-z]+", name.lower()) if len(t) > 1)
    role = re.sub(r"[^a-z]", "", str(field(summary, "role", default="")).lower())
    return " ".join(toks) + "|" + role if toks else None


# ============================================================ fraud / eligibility signals
RED_WORDS = ["unverified", "could not verify", "couldn't verify", "not verifiable", "fake",
             "fabricat", "suspicious", "inconsisten", "mismatch", "no record", "unreachable",
             "bounced", "plagiar", "red flag", "doubt", "questionable", "exaggerat",
             "copied", "cannot confirm", "no trace"]
UNAVAILABLE_WORDS = ["not looking", "not interested", "accepted another", "already joined",
                     "on hold", "withdrawn", "withdrew", "not open", "declined"]
GREEN_WORDS = ["verified", "strong reference", "referred by", "top performer", "promoted",
               "excellent reference", "confirmed"]


def heuristic_risk(profile, summary):
    """P(fabricated or ineligible) from cheap text signals. Weights are priors to tune."""
    notes = str(field(profile, "notes", "recruiter_notes", "free_text", default="")).lower()
    risk = 0.05
    risk += 0.15 * sum(w in notes for w in RED_WORDS)
    risk += 0.30 * sum(w in notes for w in UNAVAILABLE_WORDS)
    risk -= 0.04 * sum(w in notes for w in GREEN_WORDS)
    self_assess = parse_score100(field(profile, "assessment", "assessment_score"))
    if self_assess is None:
        risk += 0.20
    elif self_assess >= 98:
        risk += 0.10
    skills = skill_set(field(profile, "skills", default=field(summary, "skills")))
    if len(skills) > 20:
        risk += 0.15
    exp_p = parse_years(field(profile, "experience", "experience_years"))
    exp_s = parse_years(field(summary, "experience", "experience_years"))
    if exp_p is not None and (exp_p < 0 or exp_p > 40):
        risk += 0.30
    if exp_p is not None and exp_s is not None and abs(exp_p - exp_s) > 2:
        risk += 0.20
    if name_key(profile) and name_key(summary) and \
            name_key(profile).split("|")[0] != name_key(summary).split("|")[0]:
        risk += 0.20
    return min(0.95, max(0.01, risk))


def assessment_verdict(result):
    """(verified_score or None, ok) from an /assess response. Log the SAMPLE and adjust keys."""
    score, ok = None, True
    for k, v in (result or {}).items():
        kl, text = k.lower(), str(v).lower()
        if score is None and not isinstance(v, (bool, dict, list)) and ("assess" in kl or "score" in kl):
            score = parse_score100(v)
        if any(w in kl for w in ("fabricat", "fake", "fraud")) and v is True:
            ok = False
        if any(w in kl for w in ("reference", "verified", "genuine", "valid")):
            if v is False or any(w in text for w in ("fail", "negative", "mismatch", "not found",
                                                      "unverif", "fabricat", "fake", "could not")):
                ok = False
    return score, ok


# ============================================================ the agent
class Agent:
    def __init__(self, arena):
        self.arena = arena
        self.lock = threading.RLock()
        self.reqs = {}          # req_id -> requisition dict
        self.top = {}           # req_id -> list of summaries (top-K by summary score)
        self.profiles = {}      # cid -> compact parsed profile
        self.assessed = {}      # cid -> {"score": x, "ok": bool}
        self.llm_risk = {}      # cid -> float
        self.held = {}          # cid -> req_id
        self.dead = set()       # name keys held by someone else (or ourselves)
        self.cluster = {}       # cid -> name key
        self.llm_tokens = 0
        self.last_market_poll = 0.0
        self.pressure = {}
        self.bars = {}          # req_id -> parsed bar (cached)
        self.last_refresh = {}  # req_id -> time of last claimed-status refresh
        self.last_ledger = {}
        self.load()

    # ---------------------------------------------------------- persistence
    def save(self):
        state = {k: getattr(self, k) for k in
                 ("reqs", "top", "profiles", "assessed", "llm_risk", "held", "cluster", "llm_tokens")}
        state["dead"] = sorted(self.dead)
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, STATE_PATH)

    def load(self):
        if not os.path.exists(STATE_PATH):
            return
        try:
            with open(STATE_PATH) as f:
                s = json.load(f)
            for k in ("reqs", "top", "profiles", "assessed", "llm_risk", "held", "cluster"):
                setattr(self, k, s.get(k, {}))
            self.llm_tokens = s.get("llm_tokens", 0)
            self.dead = set(s.get("dead", []))
            log(f"restored state: {len(self.profiles)} profiles, {len(self.held)} held")
        except Exception as e:
            log(f"state file unreadable, starting fresh: {e}")

    def decide(self, action, **info):
        """Audit trail: every paid action with the reason it was worth it."""
        info.update(t=time.strftime("%H:%M:%S"), action=action, credits=self.arena.credits_remaining)
        with open(DECISIONS_PATH, "a") as f:
            f.write(json.dumps(info, default=str) + "\n")

    # ---------------------------------------------------------- phase + requisitions
    def phase(self):
        """/ledger is free: read the phase and, if it lists our signings, trust it over memory."""
        led = self.arena.ledger()
        self.last_ledger = led
        signings = field(led, "signings", "held", "signed_candidates")
        if isinstance(signings, list) and signings and isinstance(signings[0], dict):
            with self.lock:
                self.held = {str(field(s, "candidate_id", "id")): str(field(s, "req_id", "requisition"))
                             for s in signings}
        return led.get("phase", "closed")

    def refresh_reqs(self):
        raw = self.arena.requisitions()                                 # free
        items = raw if isinstance(raw, list) else field(raw, "requisitions", "results", default=[])
        for r in items:
            rid = str(field(r, "req_id", "id"))
            self.reqs[rid] = r
            self.bars[rid] = self.parse_bar(r)
            VOCAB.update(self.bars[rid]["skills"])

    def points(self, rid):
        return safe_num(field(self.reqs[rid], "points", "points_per_hire", "points_on_offer",
                              "value"), DEFAULT_REQ_POINTS)

    def open_slots(self, rid):
        r = self.reqs[rid]
        headcount = int(safe_num(field(r, "headcount"), 1))
        mine = sum(1 for v in self.held.values() if v == rid)
        remaining = field(r, "remaining", "remaining_slots", "slots_remaining")
        if remaining is not None and str(remaining).isdigit() and int(remaining) == 0:
            return 0
        return max(0, headcount - mine)

    def bar(self, rid):
        return self.bars[rid]

    @staticmethod
    def parse_bar(r):
        return {
            "min_assess": parse_score100(field(r, "min_assessment", "min_assessment_score")),
            "max_notice": parse_days(field(r, "max_notice_days", "max_notice")),
            "max_ctc": parse_lakh(field(r, "max_expected_ctc", "max_ctc", "budget")),
            "min_exp": parse_years(field(r, "min_experience", "min_experience_years")),
            "skills": skill_set(field(r, "skills", "skills_wanted", "required_skills",
                                      "must_have", default=[])),
            "role": str(field(r, "role", default="")),
            "city": field(r, "city", "location"),
        }

    # ---------------------------------------------------------- recon: wide cheap search
    def summary_score(self, s, bar):
        cov = coverage(skill_set(field(s, "skills")), bar["skills"])
        exp = parse_years(field(s, "experience", "experience_years"))
        exp_fit = 1.0 if not bar["min_exp"] or exp is None else min(1.0, exp / bar["min_exp"])
        return 0.75 * cov + 0.25 * exp_fit

    def search_role(self, role, rids):
        """Page through one role until SEARCH_PATIENCE pages add nothing to any top-K."""
        heaps = {rid: [] for rid in rids}
        bars = {rid: self.bar(rid) for rid in rids}
        stale, page = 0, 0
        while page < MAX_PAGES_PER_ROLE and (page < MIN_PAGES_PER_ROLE or stale < SEARCH_PATIENCE):
            if page % 25 == 0 and page and self.phase() != "recon":
                break                                                     # market opened
            raw = self.arena.search(role=role, page=page, size=100)
            res = raw if isinstance(raw, list) else field(raw, "results", "candidates", default=[])
            page += 1
            if not res:
                break
            improved = False
            for s in res:
                cid = str(field(s, "candidate_id", "id"))
                for rid in rids:
                    sc = self.summary_score(s, bars[rid])
                    h = heaps[rid]
                    if len(h) < TOPK_PER_REQ:
                        heapq.heappush(h, (sc, cid, json.dumps(s)))
                        improved = True
                    elif sc > h[0][0]:
                        heapq.heapreplace(h, (sc, cid, json.dumps(s)))
                        improved = True
            stale = 0 if improved else stale + 1
            if len(res) < 100:
                break
        self.decide("search", role=role, pages=page, why="1 credit per 100 summaries; stop on patience")
        for rid, h in heaps.items():
            self.top[rid] = [json.loads(x[2]) for x in sorted(h, reverse=True)]
        log(f"search role={role!r}: {page} pages, kept {[len(heaps[r]) for r in rids]}")

    def recon_search(self):
        by_role = {}
        for rid in self.reqs:
            by_role.setdefault(self.bar(rid)["role"], []).append(rid)
        for role, rids in by_role.items():
            if all(self.top.get(r) for r in rids):
                continue                                                  # restored from disk
            self.search_role(role, rids)
        for rid, lst in self.top.items():
            for s in lst:
                cid = str(field(s, "candidate_id", "id"))
                k = name_key(s)
                if k:
                    self.cluster[cid] = k
        self.save()

    # ---------------------------------------------------------- profiles
    def compact(self, p, summary):
        cid = str(field(p, "candidate_id", "id"))
        return {
            "cid": cid,
            "assess": parse_score100(field(p, "assessment", "assessment_score")),
            "notice": parse_days(field(p, "notice_period", "notice", "notice_days")),
            "ctc": parse_lakh(field(p, "expected_ctc", "ctc", "expected_salary")),
            "exp": parse_years(field(p, "experience", "experience_years")),
            "skills": sorted(skill_set(field(p, "skills", default=field(summary, "skills")))),
            "notes": str(field(p, "notes", "recruiter_notes", default=""))[:600],
            "claimed": bool(field(p, "claimed", default=False)),
            "risk": heuristic_risk(p, summary),
            "email": str(field(p, "email", "phone", default="")).lower(),
        }

    def buy_profiles(self, cids, summaries):
        """Batch when >= 30 ids (60 credits beats 30 x 2), else single calls."""
        cids = [c for c in dict.fromkeys(cids) if c not in self.profiles]
        for i in range(0, len(cids), 50):
            chunk = cids[i:i + 50]
            if len(chunk) >= 30:
                raw = self.arena.batch(chunk)
                items = raw if isinstance(raw, list) else field(
                    raw, "profiles", "candidates", "results", default=[])
                self.decide("batch", n=len(chunk), why="batch is 1.2 cr/profile")
            else:
                items = []
                for c in chunk:
                    try:
                        items.append(self.arena.candidate(c))
                    except RuntimeError as e:
                        log(f"profile {c} failed: {e}")
                self.decide("profiles", n=len(chunk), why="fewer than 30, single calls cheaper")
            for p in items:
                cid = str(field(p, "candidate_id", "id"))
                self.profiles[cid] = self.compact(p, summaries.get(cid, {}))
                if self.profiles[cid]["email"]:          # stronger duplicate key when present
                    self.cluster.setdefault(cid, "e:" + self.profiles[cid]["email"])

    def recon_profiles(self):
        summaries, wanted = {}, []
        for rid, lst in self.top.items():
            n = math.ceil(PROFILE_MULT * max(1, self.open_slots(rid)))
            seen_clusters = set()
            for s in lst:
                cid = str(field(s, "candidate_id", "id"))
                k = self.cluster.get(cid)
                if k and k in seen_clusters:
                    continue                                              # one id per person
                seen_clusters.add(k)
                summaries[cid] = s
                wanted.append(cid)
                if len(seen_clusters) >= n:
                    break
        log(f"buying {len(set(wanted))} profiles")
        self.buy_profiles(wanted, summaries)
        self.save()

    # ---------------------------------------------------------- LLM for grey-zone notes
    def llm_screen(self):
        grey = [p for p in self.profiles.values()
                if p["notes"] and 0.10 < p["risk"] < 0.6 and p["cid"] not in self.llm_risk]
        for i in range(0, len(grey), 8):
            if not USE_LLM or self.llm_tokens >= LLM_TOKEN_BUDGET:
                return
            chunk = grey[i:i + 8]
            lines = "\n".join(f'{p["cid"]}: {p["notes"][:450]}' for p in chunk)
            prompt = ("You screen recruiter notes. For each candidate id, estimate the probability "
                      "the profile is fabricated, unavailable, or misrepresented. Reply ONLY with "
                      'JSON: {"<id>": <0..1>, ...}\n\n' + lines)
            try:
                res = self.arena.reason(prompt, max_tokens=250)
            except RuntimeError as e:
                log(f"LLM unavailable ({e}); falling back to heuristics")
                return
            self.llm_tokens += int(field(res, "tokens", default=1500) or 1500)
            text = str(field(res, "completion", "text", "content", "output", default=""))
            m = re.search(r"\{.*\}", text, re.S)
            try:
                scores = json.loads(m.group()) if m else {}
            except json.JSONDecodeError:
                scores = {}
            for p in chunk:
                v = scores.get(p["cid"])
                if isinstance(v, (int, float)):
                    self.llm_risk[p["cid"]] = float(v)
            self.decide("reason", n=len(chunk), why="notes in risk grey zone; ~0.2 cr/candidate vs 25 to assess")

    # ---------------------------------------------------------- valuation
    def p_bad(self, cid):
        if cid in self.assessed:
            return 0.02 if self.assessed[cid]["ok"] else 0.99
        p = self.profiles[cid]
        r = p["risk"]
        if cid in self.llm_risk:
            r = 0.5 * r + 0.5 * self.llm_risk[cid]
        return r

    def value(self, cid, rid):
        """(expected points, P(ok)) of holding cid in rid. 0 if it clearly misses the bar."""
        p, bar = self.profiles[cid], self.bar(rid)
        a = self.assessed.get(cid, {}).get("score", p["assess"])
        unknown = 0
        if bar["min_assess"] is not None:
            if a is None:
                unknown += 1
            elif a < bar["min_assess"]:
                return 0.0, 0.0
        if bar["max_notice"] is not None:
            if p["notice"] is None:
                unknown += 1
            elif p["notice"] > bar["max_notice"]:
                return 0.0, 0.0
        if bar["max_ctc"] is not None:
            if p["ctc"] is None:
                unknown += 1
            elif p["ctc"] > bar["max_ctc"]:
                return 0.0, 0.0
        cov = coverage(set(p["skills"]), bar["skills"])
        if cov < 0.5:
            return 0.0, 0.0
        margin = 0.5 if a is None or bar["min_assess"] is None else \
            min(1.0, (a - bar["min_assess"]) / max(1.0, 100 - bar["min_assess"]))
        notice_slack = 0.5 if bar["max_notice"] in (None, 0) or p["notice"] is None else \
            1 - p["notice"] / bar["max_notice"]
        ctc_slack = 0.5 if not bar["max_ctc"] or p["ctc"] is None else 1 - p["ctc"] / bar["max_ctc"]
        quality = 0.5 * cov + 0.3 * margin + 0.1 * notice_slack + 0.1 * ctc_slack
        p_ok = max(0.0, (1 - self.p_bad(cid)) * (0.85 ** unknown))
        return p_ok * self.points(rid) * (0.5 + 0.5 * quality), p_ok

    def available(self, cid):
        k = self.cluster.get(cid)
        return cid not in self.held and not self.profiles[cid]["claimed"] and not (k and k in self.dead)

    def ranked(self, rid):
        """Candidates for rid, best first, that clear the sign rule."""
        out = []
        offer_cost = COST["offer"] * PENALTY_FACTOR
        for cid in self.profiles:
            if not self.available(cid):
                continue
            ev, p_ok = self.value(cid, rid)
            if p_ok >= P_OK_MIN and ev > offer_cost:
                out.append((ev, cid))
        out.sort(reverse=True)
        return out

    # ---------------------------------------------------------- assessments
    def worth_assessing(self, cid, rid):
        """Value of information: P(bad) x slot value vs 25 x PF."""
        if cid in self.assessed:
            return False
        slot_value = self.points(rid)
        return self.p_bad(cid) * slot_value > COST["assess"] * PENALTY_FACTOR

    def assess(self, cid, why):
        try:
            res = self.arena.assess(cid)
        except RuntimeError as e:
            log(f"assess {cid} failed: {e}")
            return None
        score, ok = assessment_verdict(res)
        self.assessed[cid] = {"score": score, "ok": ok}
        self.decide("assess", cid=cid, ok=ok, score=score, why=why)
        return ok

    def recon_assess(self):
        for rid in self.reqs:
            budget = math.ceil(ASSESS_MULT * self.open_slots(rid))
            for ev, cid in self.ranked(rid)[: budget * 2]:
                if budget <= 0:
                    break
                if self.worth_assessing(cid, rid):
                    self.assess(cid, why=f"pre-market, top of {rid} queue, p_bad={self.p_bad(cid):.2f}")
                    budget -= 1
        self.save()

    # ---------------------------------------------------------- offers
    def try_offer(self, cid, rid, closing=False):
        cost = COST["offer_closing" if closing else "offer"]
        with self.lock:
            if not self.available(cid) or self.open_slots(rid) <= 0:
                return False
            self.held[cid] = rid                                 # reserve locally before the call
        try:
            res = self.arena.offer(cid, rid)
        except (RuntimeError, WrongPhase) as e:
            with self.lock:
                self.held.pop(cid, None)
            log(f"offer {cid}->{rid} error: {e}")
            return False
        reason = res.get("reason")
        with self.lock:
            if res.get("accepted"):
                k = self.cluster.get(cid)
                if k:
                    self.dead.add(k)                             # never offer this person again
                self.decide("offer", cid=cid, req=rid, cost=cost, accepted=True,
                            why=f"ev={self.value(cid, rid)[0]:.1f} > {cost}x{PENALTY_FACTOR}")
                log(f"SIGNED {cid} -> {rid}")
                return True
            self.held.pop(cid, None)
            if reason in ("already_signed", "same_person_already_signed"):
                k = self.cluster.get(cid)
                if k:
                    self.dead.add(k)
                self.profiles[cid]["claimed"] = True
            elif reason == "requisition_full":
                self.reqs[rid]["remaining"] = 0
            self.decide("offer", cid=cid, req=rid, accepted=False, reason=reason)
            return False

    def fill_req(self, rid, closing=False):
        """Walk rid's queue until its slots are full or the queue clears no more."""
        for ev, cid in self.ranked(rid):
            if self.open_slots(rid) <= 0:
                return
            cost = COST["offer_closing" if closing else "offer"] * PENALTY_FACTOR
            if ev <= cost:
                return
            self.try_offer(cid, rid, closing)

    def opening_burst(self):
        """11:30: one worker per requisition, all firing at once (throttle keeps us legal)."""
        workers = [threading.Thread(target=self.fill_req, args=(rid,), daemon=True) for rid in self.reqs]
        for w in workers:
            w.start()
        for w in workers:
            w.join()
        self.save()
        log(f"burst done: holding {len(self.held)}")

    # ---------------------------------------------------------- market loop
    def refresh_stale(self, rid, every_s=600):
        """Later in the market P(claimed) is high. Re-check the head of the queue (batch = 1.2 cr
        each, single = 2) at most every 10 min, instead of paying 10 per already_signed offer."""
        if time.time() - self.last_refresh.get(rid, 0) < every_s:
            return
        self.last_refresh[rid] = time.time()
        ids = [cid for _, cid in self.ranked(rid)[:max(3, 2 * self.open_slots(rid))]]
        if not ids:
            return
        if len(ids) >= 30:
            raw = self.arena.batch(ids[:50])
            items = raw if isinstance(raw, list) else field(
                raw, "profiles", "candidates", "results", default=[])
        else:
            items = []
            for c in ids:
                try:
                    items.append(self.arena.candidate(c))
                except RuntimeError:
                    pass
        for p in items:
            cid = str(field(p, "candidate_id", "id"))
            if cid in self.profiles:
                self.profiles[cid]["claimed"] = bool(field(p, "claimed", default=False))
        self.decide("refresh", req=rid, n=len(ids), why="avoid paying 10 for already_signed")

    def verify_holds(self):
        for cid, rid in list(self.held.items()):
            if cid in self.profiles and self.worth_assessing(cid, rid):
                ok = self.assess(cid, why="post-sign check, no race now")
                if ok is False or self.value(cid, rid)[0] == 0:
                    self.arena.release(cid)
                    self.held.pop(cid, None)
                    self.decide("release", cid=cid, why="failed verified assessment")

    def maybe_upgrade(self, rid):
        if self.open_slots(rid) > 0:
            return
        mine = [(self.value(c, rid)[0], c) for c, r in self.held.items() if r == rid and c in self.profiles]
        best = self.ranked(rid)[:1]
        if not mine or not best:
            return
        worst_ev, worst = min(mine)
        gain = best[0][0] - worst_ev - (COST["release"] + COST["offer"]) * PENALTY_FACTOR
        if gain > UPGRADE_MIN_GAIN:
            self.arena.release(worst)
            self.held.pop(worst, None)
            self.decide("release", cid=worst, why=f"upgrade gain {gain:.1f} pts")
            self.try_offer(best[0][1], rid)

    def expand_pool(self, rid):
        """Queue ran dry but slots remain: buy the next profiles from the kept summaries."""
        summaries = {str(field(s, "candidate_id", "id")): s for s in self.top.get(rid, [])}
        fresh = [c for c in summaries if c not in self.profiles and
                 not (self.cluster.get(c) in self.dead)][:50]
        if fresh:
            est_gain = self.points(rid) * 0.5
            if est_gain > COST["batch"] * PENALTY_FACTOR:
                self.buy_profiles(fresh, summaries)

    def poll_market(self):
        if time.time() - self.last_market_poll < MARKET_POLL_S:
            return
        self.last_market_poll = time.time()
        m = self.arena.market()
        self.pressure = field(m, "price_pressure", "pressure", default={}) or {}
        log(f"market: rank={field(m, 'rank', 'your_rank')} leader={field(m, 'leader_score')}")

    def market_loop(self):
        started_market = False
        while True:
            try:
                ph = self.phase()
                if ph not in ("market", "closing"):
                    return
                closing = ph == "closing"
                if not started_market and not closing:
                    self.opening_burst()
                    started_market = True
                self.refresh_reqs()
                open_reqs = [r for r in self.reqs if self.open_slots(r) > 0]
                if open_reqs and not closing:
                    self.poll_market()
                pressure = self.pressure if isinstance(self.pressure, dict) else {}
                open_reqs.sort(key=lambda r: -float(pressure.get(r, 0) or 0))
                for rid in open_reqs:
                    if not closing:
                        self.refresh_stale(rid)
                    self.fill_req(rid, closing)
                    if self.open_slots(rid) > 0 and not self.ranked(rid) and not closing:
                        self.expand_pool(rid)
                if not closing:
                    self.verify_holds()
                    for rid in self.reqs:
                        self.maybe_upgrade(rid)
                self.save()
            except (Exhausted, WrongPhase):
                raise
            except Exception:
                log("loop error (continuing):\n" + traceback.format_exc())
            time.sleep(LOOP_SLEEP_S)

    def tune_knobs(self):
        """How many credits is one hire worth? points / PF. Cheap credits -> explore more."""
        global PROFILE_MULT, ASSESS_MULT, MIN_PAGES_PER_ROLE
        pts = [self.points(r) for r in self.reqs] or [DEFAULT_REQ_POINTS]
        worth = (sum(pts) / len(pts)) / max(PENALTY_FACTOR, 1e-9)
        if worth >= 1000:
            tier, knobs = "explore", (8.0, 3.0, 150)
        elif worth < 200:
            tier, knobs = "frugal", (3.0, 1.0, 50)
        else:
            tier, knobs = "balanced", (PROFILE_MULT, ASSESS_MULT, MIN_PAGES_PER_ROLE)
        if "PROFILE_MULT" not in os.environ:
            PROFILE_MULT = knobs[0]
        if "ASSESS_MULT" not in os.environ:
            ASSESS_MULT = knobs[1]
        if "MIN_PAGES_PER_ROLE" not in os.environ:
            MIN_PAGES_PER_ROLE = knobs[2]
        log(f"TUNE points/hire~{sum(pts) / len(pts):.0f} PF={PENALTY_FACTOR} -> one hire worth "
            f"{worth:.0f} credits -> tier={tier} PROFILE_MULT={PROFILE_MULT} "
            f"ASSESS_MULT={ASSESS_MULT} MIN_PAGES_PER_ROLE={MIN_PAGES_PER_ROLE}")
        self.decide("tune", tier=tier, worth_credits=round(worth), why="points per hire / PF")

    # ---------------------------------------------------------- run
    def wait_while(self, phases, poll=2):
        while True:
            try:
                ph = self.phase()
                if ph not in phases:
                    return ph
            except RuntimeError as e:
                log(f"ledger unreachable, retrying: {e}")
            time.sleep(poll)

    def run(self):
        log(f"PF={PENALTY_FACTOR} RPS={RPS} PROFILE_MULT={PROFILE_MULT}")
        try:
            ph = self.wait_while(("closed",), poll=5)
            self.refresh_reqs()
            log(f"phase {ph}: {len(self.reqs)} requisitions, {self.arena.credits_remaining} credits")
            self.tune_knobs()
            if ph == "recon" or not self.profiles:
                for step in (self.recon_search, self.recon_profiles, self.llm_screen, self.recon_assess):
                    try:
                        step()
                    except (Exhausted, WrongPhase):
                        raise
                    except Exception:
                        log(f"{step.__name__} failed, continuing:\n" + traceback.format_exc())
                log(f"recon done: {len(self.profiles)} profiles, {len(self.assessed)} assessed, "
                    f"credits left {self.arena.credits_remaining}")
            self.wait_while(("recon",), poll=1)
            self.market_loop()
        except Exhausted:
            log("out of credits: holding what we have")
        except WrongPhase as e:
            log(f"phase refused a call: {e}")
        finally:
            self.save()
            try:
                led = self.arena.ledger()
                log(f"FINAL ledger: {json.dumps(led, default=str)[:500]}")
            except Exception:
                pass


if __name__ == "__main__":
    Agent(ThrottledArena()).run()
