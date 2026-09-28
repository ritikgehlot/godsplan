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

Robustness
  - Response field names are unconfirmed, so every read goes through field()/as_items()
    with several candidate names and accepts bare-list or dict-wrapped shapes.
  - Slot counts trust the server (/requisitions filled/remaining, /ledger signings) over
    memory, so a restart never pays for requisition_full.
  - run() is a supervisor loop: only Exhausted (out of credits) or the arena closing ends
    it; a 409 just makes it re-read the phase. A missing phase field is logged loudly and
    treated as recon; a 409 on a 1-credit probe then tells us the arena is closed.
  - State is checkpointed to disk. With the file, a restart resumes; without it, mid-market,
    the agent rebuilds holdings from the server and runs a short recon before trading.
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
RESTART_PAGES_PER_ROLE = _env("RESTART_PAGES_PER_ROLE", 25, int)  # quick recon after a fresh-disk restart
P_OK_MIN = _env("P_OK_MIN", 0.6)                  # never sign below this estimated P(eligible)
UPGRADE_MIN_GAIN = _env("UPGRADE_MIN_GAIN", 10.0)  # points; release+re-sign only above this
USE_LLM = _env("USE_LLM", 1, int)
LLM_TOKEN_BUDGET = _env("LLM_TOKEN_BUDGET", 120_000, int)  # hard cap is 450K
MARKET_POLL_S = _env("MARKET_POLL_S", 300, int)   # /market costs 2; only while slots are open
LOOP_SLEEP_S = _env("LOOP_SLEEP_S", 5, int)
PROBE_S = _env("PROBE_S", 30, int)                # phase field missing: 1-credit open/closed probe interval
RPS = _env("RPS", 8.0)                            # stay under 10/s (free calls count too)
STATE_PATH = os.environ.get("STATE_PATH", "agent_state.json")
DECISIONS_PATH = os.environ.get("DECISIONS_PATH", "decisions.jsonl")

COST = {"search": 1, "profile": 2, "batch": 60, "assess": 25, "offer": 10,
        "offer_closing": 20, "release": 5, "market": 2}
SAMPLES_PER_ENDPOINT = 4                          # distinct response shapes logged per endpoint


def log(*msg):
    print(time.strftime("%H:%M:%S"), *msg, flush=True)


# ============================================================ throttled client
class ThrottledArena(Arena):
    """Spaces every call (free ones too) so the team never hits 429 rate_limited, turns a
    malformed JSON body into a RuntimeError, and logs each new response shape once (SAMPLE)."""

    def __init__(self, rps=RPS, **kw):
        super().__init__(**kw)
        self._gap, self._next, self._tlock = 1.0 / rps, 0.0, threading.Lock()
        self.samples_logged = {}

    def _call(self, path, *a, **kw):
        with self._tlock:
            now = time.monotonic()
            wait = self._next - now
            self._next = max(now, self._next) + self._gap
        if wait > 0:
            time.sleep(wait)
        try:
            result = super()._call(path, *a, **kw)
        except ValueError as e:                            # JSONDecodeError / bad bytes on a 2xx
            raise RuntimeError(f"malformed JSON from {path.split('?')[0]}: {e}") from None
        self._sample(path, result)
        return result

    def _sample(self, path, result):
        endpoint = path.split("?")[0].strip("/").split("/")[0] or path
        shape = tuple(sorted(map(str, result))) if isinstance(result, dict) else type(result).__name__
        seen = self.samples_logged.setdefault(endpoint, set())
        if shape in seen or len(seen) >= SAMPLES_PER_ENDPOINT:
            return
        seen.add(shape)
        log(f"SAMPLE {endpoint}: {json.dumps(result, default=str)[:700]}")


# ============================================================ response shapes + field names
# Field names are NOT confirmed by real samples yet: real name candidates first, old guesses kept.
ID_KEYS = ("candidate_id", "id", "cid", "candidateId")
REQ_KEYS = ("req_id", "id", "requisition_id", "reqId")
PHASE_KEYS = ("phase", "status", "state", "arena_phase", "stage")
F_ASSESS = ("assessment", "assessment_score", "self_assessment", "self_reported_assessment")
F_NOTICE = ("notice_period", "notice", "notice_days", "notice_period_days")
F_CTC = ("expected_ctc", "ctc", "expected_salary", "expected_ctc_lpa")
F_EXP = ("experience", "experience_years", "years_experience", "yoe")
F_NOTES = ("notes", "recruiter_notes", "free_text", "remarks")
F_CLAIMED = ("claimed", "is_claimed", "taken")
FALSY_WORDS = {"", "false", "no", "n", "f", "0", "0.0", "none", "null", "nil", "unclaimed",
               "available", "free"}


def field(d, *names, default=None):
    """First present, non-empty value among several possible key names."""
    for n in names:
        if isinstance(d, dict) and d.get(n) not in (None, "", []):
            return d[n]
    return default


def truthy(v):
    """A server flag that may be bool, number or string: 'false'/'no'/'0'/''/null -> False."""
    if v is None:
        return False
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, (list, dict)):
        return len(v) > 0
    return str(v).strip().lower() not in FALSY_WORDS


def rec_id(rec, keys=ID_KEYS):
    """The record's id as a string, or None."""
    v = field(rec, *keys)
    return None if v is None or isinstance(v, (dict, list, bool)) else str(v)


def as_items(raw, *keys):
    """Records from a bare list, a dict wrapping them under one of `keys`, or a dict keyed by id."""
    if isinstance(raw, list):
        return [x for x in raw if isinstance(x, dict)]
    if not isinstance(raw, dict):
        return []
    for k in keys:
        if isinstance(raw.get(k), (list, dict)):
            return as_items(raw[k])
    if raw and all(isinstance(v, dict) for v in raw.values()):
        return [v if rec_id(v) or rec_id(v, REQ_KEYS) else dict(v, id=k) for k, v in raw.items()]
    return []


def unwrap(raw, *keys):
    """A single record, or the one wrapped under one of `keys`."""
    if isinstance(raw, list):
        return raw[0] if raw and isinstance(raw[0], dict) else {}
    if not isinstance(raw, dict):
        return {}
    for k in keys:
        if isinstance(raw.get(k), dict):
            return raw[k]
    return raw


def norm_phase(v):
    """'MARKET', 'market_open', 'Closing' -> recon/market/closing/closed; unrecognised -> None."""
    if not isinstance(v, str):
        return None
    s = v.strip().lower()
    if "recon" in s:
        return "recon"
    if "closing" in s:
        return "closing"
    if "market" in s or "trading" in s:
        return "market"
    if "closed" in s or s in ("ended", "finished", "final", "over", "not_started", "pending",
                               "waiting", "pre", "prestart", "pre_start"):
        return "closed"
    return None


def is_last_page(raw, n_this, first_len, seen_total):
    """has_more=false, or all `total` results seen, or a page shorter than page 0."""
    if isinstance(raw, dict):
        more = field(raw, "has_more", "hasMore", "has_next", "more")
        if more is not None:
            return not truthy(more)
        total = safe_num(field(raw, "total", "total_results", "total_count"), None)
        if total is not None:
            return seen_total >= total
    return n_this < first_len


REJECT_REASONS = ("same_person_already_signed", "already_signed", "requisition_full", "role_mismatch")


def offer_reason(text):
    """Known rejection reason inside any text (response field or HTTP error body), else None."""
    s = re.sub(r"[\s\-]+", "_", str(text or "").lower())
    for r in REJECT_REASONS:
        if r in s:
            return r
    return None


def offer_outcome(res):
    """(accepted, already_yours, reason) from an /offer response of any known shape."""
    res = unwrap(res, "offer", "result")
    acc = field(res, "accepted", "ok", "success", "signed")
    if acc is None:
        acc = str(field(res, "status", "result", default="")).lower() in (
            "accepted", "signed", "ok", "success", "hired")
    raw_reason = field(res, "reason", "error", "detail", "message", "status")
    reason = offer_reason(raw_reason)
    accepted = truthy(acc) and reason is None
    if not accepted and reason is None and raw_reason is not None:
        reason = str(raw_reason)[:60]
    return accepted, truthy(field(res, "already_yours")), reason


# ============================================================ parsing messy values
NUM = re.compile(r"\d+(?:\.\d+)?")
WORD_NUMS = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
             "a month": "1 month", "half": "0.5"}
ZERO_NOTICE = re.compile(r"immediate|available now|can join now|no notice|\bnone\b|\bnil\b|\bzero\b")


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
    """Notice period in days: 'immediate'/'none'->0, '2 months'->60, '3 weeks'->21, 45->45."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).lower()
    for w, n in WORD_NUMS.items():
        s = re.sub(rf"\b{w}\b", n, s)
    m = NUM.search(s)
    if not m:
        return 0.0 if ZERO_NOTICE.search(s) else None
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
    monthly = re.search(r"month|/\s*mo\b|\bpm\b|p\.m\b", s) is not None
    if re.search(r"\d\s*cr\b|crore", s):
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
# Slash-joined names that are one skill, protected before splitting on '/'.
COMPOUND_SKILLS = re.compile(r"\b(ci)\s*/\s*(cd)\b|\b(tcp)\s*/\s*(ip)\b|\b(pl)\s*/\s*(sql)\b", re.I)
VOCAB = set()          # every skill any requisition asks for; filled after /requisitions
_canon_cache = {}


def skill_key(raw):
    k = re.sub(r"\(.*?\)", "", str(raw).lower())               # 'Python (5 yrs)' -> 'python'
    k = re.sub(r"[\s\.\-_]", "", k.strip())
    return ALIASES.get(k, k)


def skill_set(raw):
    """Normalised skill keys from a string, a list of strings/dicts, or a {skill: weight} dict."""
    if not raw:
        return set()
    if isinstance(raw, dict):
        items = list(raw)
    elif isinstance(raw, list):
        items = [field(s, "name", "skill", default="") if isinstance(s, dict) else s for s in raw]
    else:
        joined = COMPOUND_SKILLS.sub(lambda m: "".join(g for g in m.groups() if g), str(raw))
        items = re.split(r"[,;|/]", joined)
    return {skill_key(s) for s in items if str(s).strip()} - {""}


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
    name = str(field(summary, "name", "full_name", "candidate_name", default=""))
    toks = sorted(t for t in re.split(r"[^a-z]+", name.lower()) if len(t) > 1)
    role = re.sub(r"[^a-z]", "", str(field(summary, "role", default="")).lower())
    return " ".join(toks) + "|" + role if toks else None


# ============================================================ fraud / eligibility signals
RED_WORDS = ["unverified", "could not verify", "couldn't verify", "not verifiable", "fake",
             "fabricat", "suspicious", "inconsisten", "mismatch", "no record", "unreachable",
             "bounced", "plagiar", "red flag", "doubt", "questionable", "exaggerat",
             "copied", "cannot confirm", "no trace", "unable to verify", "failed verification"]
UNAVAILABLE_WORDS = ["not looking", "not interested", "accepted another", "already joined",
                     "on hold", "withdrawn", "withdrew", "not open", "declined"]
GREEN_RE = re.compile(r"\b(?:verified|strong reference|referred by|top performer|promoted|"
                      r"excellent reference|confirmed)\b")
# "not verified", "could not be confirmed" ... are removed before counting green words.
NEGATED_GREEN = re.compile(r"\b(?:not|never|unable to|cannot|can't|couldn't|could not|failed to)"
                           r"\b[\w ]{0,15}?\b(?:verified|confirmed)\b")


def notes_text(profile):
    notes = field(profile, *F_NOTES, default="")
    return " ".join(map(str, notes)) if isinstance(notes, list) else str(notes)


def heuristic_risk(profile, summary):
    """P(fabricated or ineligible) from cheap text signals. Weights are priors to tune."""
    notes = notes_text(profile).lower()
    risk = 0.05
    risk += 0.15 * (sum(w in notes for w in RED_WORDS) + len(NEGATED_GREEN.findall(notes)))
    risk += 0.30 * sum(w in notes for w in UNAVAILABLE_WORDS)
    risk -= 0.04 * len(GREEN_RE.findall(NEGATED_GREEN.sub(" ", notes)))
    self_assess = parse_score100(field(profile, *F_ASSESS))
    if self_assess is None:
        risk += 0.20
    elif self_assess >= 98:
        risk += 0.10
    skills = skill_set(field(profile, "skills", default=field(summary, "skills")))
    if len(skills) > 20:
        risk += 0.15
    exp_p = parse_years(field(profile, *F_EXP))
    exp_s = parse_years(field(summary, *F_EXP))
    if exp_p is not None and (exp_p < 0 or exp_p > 40):
        risk += 0.30
    if exp_p is not None and exp_s is not None and abs(exp_p - exp_s) > 2:
        risk += 0.20
    if name_key(profile) and name_key(summary) and \
            name_key(profile).split("|")[0] != name_key(summary).split("|")[0]:
        risk += 0.20
    return min(0.95, max(0.01, risk))


FAIL_WORDS = ("fail", "negative", "mismatch", "not found", "unverif", "fabricat", "fake", "fraud",
              "could not", "cannot", "inconsisten", "discrepan", "red flag", "reject")
NEGATED_FAIL = re.compile(r"\bno\s+(?:\w+\s+){0,2}?(?:mismatch\w*|issues?|concerns?|red flags?|"
                          r"discrepanc\w*|fraud|problems?)\b|\bnot (?:fake|fabricated)\b")
VERDICT_KEYS = ("reference", "verif", "genuine", "valid", "pass", "authentic", "status", "result",
                "verdict", "flag", "check")


def _flatten(d, prefix=""):
    out = {}
    for k, v in d.items():
        key = f"{prefix}{k}".lower()
        if isinstance(v, dict):
            out.update(_flatten(v, key + "."))
        else:
            out[key] = v
    return out


def assessment_verdict(result):
    """(verified_score or None, ok) from an /assess response of unknown shape.
    Score: prefer keys naming a verified assessment, never self-reported/reference scores.
    Not ok: any fraud flag set, or any verification/reference field that is False or says fail."""
    flat = _flatten(result) if isinstance(result, dict) else {}
    scored = [(("verif" in k) + ("assess" in k), k, v) for k, v in flat.items()
              if not isinstance(v, (bool, list)) and ("assess" in k or "score" in k)
              and not any(w in k for w in ("self", "claim", "reported", "reference", "fraud", "risk"))]
    score = None
    for _, _, v in sorted(scored, key=lambda t: -t[0]):
        score = parse_score100(v)
        if score is not None:
            break
    ok = True
    for k, v in flat.items():
        text = NEGATED_FAIL.sub(" ", (" ".join(map(str, v)) if isinstance(v, list) else str(v)).lower())
        if any(w in k for w in ("fabricat", "fake", "fraud", "suspicious")):
            bad = safe_num(v, 0) >= 0.5 if isinstance(v, (int, float)) and not isinstance(v, bool) \
                else truthy(v) and not any(w in text for w in ("pass", "clear", "clean"))
            if bad:
                ok = False
        elif any(w in k for w in VERDICT_KEYS):
            if v is False or (isinstance(v, (str, list)) and any(w in text for w in FAIL_WORDS)):
                ok = False
    return score, ok


# ============================================================ the agent
DICT_STATE = ("reqs", "top", "profiles", "assessed", "llm_risk", "held", "cluster")


class Agent:
    def __init__(self, arena):
        self.arena = arena
        self.lock = threading.RLock()
        self.io_lock = threading.Lock()
        self.reset_state()
        self.bars = {}          # req_id -> parsed bar (cached)
        self.filled_srv = {}    # req_id -> slots the server says we filled (adjusted on release)
        self.pending = set()    # cids with an offer in flight
        self.last_refresh = {}  # req_id -> time of last claimed-status refresh
        self.last_ledger = {}
        self.last_market_poll = 0.0
        self.last_phase_warn = 0.0
        self.last_probe, self.probe_result = 0.0, None
        self.pressure = {}
        self.burst_error = None
        self.setup_done = self.recon_done = self.started_market = False
        self.saved_arena = None
        self.load()

    def reset_state(self):
        self.reqs = {}          # req_id -> requisition dict
        self.top = {}           # req_id -> list of summaries (top-K by summary score)
        self.profiles = {}      # cid -> compact parsed profile
        self.assessed = {}      # cid -> {"score": x, "ok": bool}
        self.llm_risk = {}      # cid -> float
        self.held = {}          # cid -> req_id
        self.cluster = {}       # cid -> name key
        self.dead = set()       # name keys held by someone else (or ourselves)
        self.released = set()   # cids we released: ignore them if a stale ledger still lists them
        self.bad_pairs = set()  # "cid|rid" rejected with role_mismatch: never offer again
        self.llm_tokens = 0

    # ---------------------------------------------------------- persistence
    def save(self):
        try:
            with self.lock:
                state = {k: getattr(self, k) for k in DICT_STATE}
                state.update(dead=sorted(self.dead), released=sorted(self.released),
                             bad_pairs=sorted(self.bad_pairs), llm_tokens=self.llm_tokens,
                             arena=self.arena.base)
                blob = json.dumps(state, default=str)
            tmp = STATE_PATH + ".tmp"
            with open(tmp, "w") as f:
                f.write(blob)
            os.replace(tmp, STATE_PATH)
        except Exception as e:                                  # a disk problem must not stop trading
            log(f"state save failed (continuing): {e}")

    def load(self):
        if not os.path.exists(STATE_PATH):
            log("no state file: fresh start")
            return
        try:
            with open(STATE_PATH) as f:
                s = json.load(f)
            if not isinstance(s, dict):
                raise ValueError("state is not a JSON object")
            for k in DICT_STATE:
                setattr(self, k, s[k] if isinstance(s.get(k), dict) else {})
            self.dead = set(s.get("dead") or [])
            self.released = set(s.get("released") or [])
            self.bad_pairs = set(s.get("bad_pairs") or [])
            self.llm_tokens = int(safe_num(s.get("llm_tokens"), 0))
            self.saved_arena = s.get("arena")
            log(f"restored state: {len(self.profiles)} profiles, {len(self.held)} held")
        except Exception as e:
            log(f"state file unreadable, starting fresh: {e}")
            self.reset_state()

    def check_state(self, saved_req_ids):
        """Drop a state file that belongs to another arena (e.g. left over from a local test)."""
        other_url = self.saved_arena not in (None, self.arena.base)
        disjoint = bool(saved_req_ids) and not (saved_req_ids & set(self.reqs))
        if other_url or disjoint:
            log(f"state file is from another arena (url_differs={other_url}, reqs_disjoint={disjoint}): discarding")
            self.reset_state()
            self.refresh_reqs()

    def decide(self, action, **info):
        """Audit trail: every paid action with the reason it was worth it."""
        info.update(t=time.strftime("%H:%M:%S"), action=action, credits=self.arena.credits_remaining)
        try:
            with self.io_lock, open(DECISIONS_PATH, "a") as f:
                f.write(json.dumps(info, default=str) + "\n")
        except Exception as e:
            log(f"decision log write failed (continuing): {e}")

    # ---------------------------------------------------------- phase + server truth
    def phase(self):
        """recon/market/closing/closed from /ledger (free), or 'unknown' if it has no phase field.
        Also merges the ledger's list of our signings into self.held (server truth)."""
        led = self.arena.ledger()
        self.last_ledger = led if isinstance(led, dict) else {}
        self.merge_signings(field(self.last_ledger, "signings", "held", "signed_candidates",
                                  "holdings", "hires", "roster", "signed"))
        for k in PHASE_KEYS:
            ph = norm_phase(self.last_ledger.get(k))
            if ph:
                return ph
        if time.time() - self.last_phase_warn > 60:
            self.last_phase_warn = time.time()
            log(f"WARNING: /ledger has no recognisable phase (tried {PHASE_KEYS}; got keys "
                f"{sorted(map(str, self.last_ledger))[:20]}). Acting as recon; a 409 means closed.")
        return "unknown"

    def probe_phase(self):
        """Phase unknown: a 1-credit search answers 409 only when the arena is closed."""
        if self.probe_result is None or time.time() - self.last_probe >= PROBE_S:
            self.last_probe = time.time()
            try:
                self.arena.search(page=0, size=1)
                self.probe_result = "open"
            except WrongPhase:
                self.probe_result = "closed"
            self.decide("probe", result=self.probe_result, why="ledger has no phase field")
        return self.probe_result

    def merge_signings(self, raw):
        """Add signings the server lists but we forgot (restart / fresh disk). Add-only, so a
        ledger that lags never frees a slot we still hold; our own releases are excluded."""
        entries = []
        if isinstance(raw, list):
            for s in raw:
                if isinstance(s, dict):
                    entries.append((rec_id(s), field(s, "req_id", "requisition_id", "requisition", "req")))
                elif isinstance(s, (str, int)):
                    entries.append((str(s), None))
        elif isinstance(raw, dict):
            for k, v in raw.items():
                if isinstance(v, list):                                     # {req_id: [cids]}
                    entries += [(x if isinstance(x, str) else rec_id(x), k) for x in v]
                elif isinstance(v, str):                                    # {cid: req} or {req: cid}
                    entries.append((v, k) if k in self.reqs else (k, v))
        with self.lock:
            for cid, rid in entries:
                if cid and cid not in self.released and cid not in self.held:
                    self.held[str(cid)] = str(rid) if rid is not None else ""

    def arena_finished(self):
        """Closed at start-up after credits were spent means the arena is over, not pending."""
        return safe_num(field(self.last_ledger, "credits_used", "used_credits", "spent"), 0) > 0

    def refresh_reqs(self):
        raw = self.arena.requisitions()                                 # free
        fresh = {}
        for r in as_items(raw, "requisitions", "results", "items", "data"):
            rid = rec_id(r, REQ_KEYS)
            if not rid:
                continue
            fresh[rid] = r
            self.bars[rid] = self.parse_bar(r)
            VOCAB.update(self.bars[rid]["skills"])
            filled, ids = self.server_filled(r)
            if filled is not None:
                self.filled_srv[rid] = filled
            self.merge_signings({rid: ids} if ids else None)
        if fresh:
            self.reqs = fresh
        else:
            log(f"WARNING: /requisitions gave no usable records: {str(raw)[:200]}")

    @staticmethod
    def headcount(r):
        return int(safe_num(field(r, "headcount", "slots", "openings", "positions", "vacancies"), 1))

    def server_filled(self, r):
        """(slots we have filled per the server or None, candidate ids it lists as ours)."""
        hc, ids, n = self.headcount(r), [], None
        filled = field(r, "filled", "your_filled", "filled_slots", "signed", "hired")
        if isinstance(filled, list):
            ids = [x if isinstance(x, str) else rec_id(x) for x in filled]
            ids, n = [i for i in ids if i], len(filled)
        elif filled is not None and not isinstance(filled, dict):
            n = safe_num(filled, None)
        rem = safe_num(field(r, "remaining", "your_remaining", "remaining_slots", "slots_remaining",
                             "open_slots"), None)
        if rem is not None:
            n = max(n or 0, hc - rem)
        return (None if n is None else max(0, int(n))), ids

    def points(self, rid):
        return safe_num(field(self.reqs[rid], "points", "points_per_hire", "points_on_offer",
                              "value", "reward"), DEFAULT_REQ_POINTS)

    def open_slots(self, rid):
        """Headcount minus slots in use. Slots in use = the server's filled count (kept live between
        refreshes by our own accepts/releases) plus offers in flight, or our local holds if more.
        After a fresh-disk restart local holds miss earlier signings, so the server count wins."""
        if rid not in self.reqs:
            return 0
        held = list(self.held.items())
        mine = sum(1 for _, r in held if r == rid)
        if rid in self.filled_srv:
            in_flight = sum(1 for c, r in held if r == rid and c in self.pending)
            mine = max(mine, self.filled_srv[rid] + in_flight)
        return max(0, self.headcount(self.reqs[rid]) - mine)

    def bar(self, rid):
        return self.bars[rid]

    @staticmethod
    def parse_bar(r):
        src = dict(r)
        for k in ("bar", "requirements", "criteria"):
            if isinstance(r.get(k), dict):
                src.update(r[k])
        return {
            "min_assess": parse_score100(field(src, "min_assessment", "min_assessment_score", "min_score")),
            "max_notice": parse_days(field(src, "max_notice_days", "max_notice", "notice_days")),
            "max_ctc": parse_lakh(field(src, "max_expected_ctc", "max_ctc", "budget", "ctc_budget")),
            "min_exp": parse_years(field(src, "min_experience", "min_experience_years")),
            "skills": skill_set(field(src, "skills", "skills_wanted", "required_skills",
                                      "must_have", default=[])),
            "role": str(field(src, "role", "title", default="")),
            "city": field(src, "city", "location"),
        }

    # ---------------------------------------------------------- recon: wide cheap search
    def summary_score(self, s, bar):
        cov = coverage(skill_set(field(s, "skills")), bar["skills"])
        exp = parse_years(field(s, *F_EXP))
        exp_fit = 1.0 if not bar["min_exp"] or exp is None else min(1.0, exp / bar["min_exp"])
        return 0.75 * cov + 0.25 * exp_fit

    def search_page(self, role, page, tries=3):
        """One search page; a malformed or failed page is retried, then skipped (None)."""
        for attempt in range(tries):
            try:
                return self.arena.search(role=role, page=page, size=100)
            except (Exhausted, WrongPhase):
                raise
            except Exception as e:
                log(f"search {role!r} page {page} failed (attempt {attempt + 1}): {e}")
        return None

    def search_role(self, role, rids, max_pages=None):
        """Page through one role until SEARCH_PATIENCE pages add nothing to any top-K."""
        quick = max_pages is not None
        max_pages = max_pages or MAX_PAGES_PER_ROLE
        min_pages = 0 if quick else MIN_PAGES_PER_ROLE
        heaps = {rid: [] for rid in rids}
        bars = {rid: self.bar(rid) for rid in rids}
        stale, page, first_len, seen = 0, 0, 0, 0
        while page < max_pages and (page < min_pages or stale < SEARCH_PATIENCE):
            if not quick and page % 25 == 0 and page and self.phase() not in ("recon", "unknown"):
                break                                                     # market opened
            raw = self.search_page(role, page)
            page += 1
            if raw is None:                                               # unreadable after retries
                stale += 1
                continue
            res = as_items(raw, "results", "candidates", "items", "data", "summaries")
            if not res:
                break
            first_len, seen = first_len or len(res), seen + len(res)
            improved = False
            for s in res:
                cid = rec_id(s)
                if not cid:
                    continue
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
            if is_last_page(raw, len(res), first_len, seen):
                break
        self.decide("search", role=role, pages=page, quick=quick,
                    why="1 credit per 100 summaries; stop on patience or last page")
        for rid, h in heaps.items():
            self.top[rid] = [json.loads(x[2]) for x in sorted(h, reverse=True)]
        log(f"search role={role!r}: {page} pages, kept {[len(heaps[r]) for r in rids]}")

    def recon_search(self, quick=False):
        by_role = {}
        for rid in self.reqs:
            by_role.setdefault(self.bar(rid)["role"], []).append(rid)
        for role, rids in by_role.items():
            if all(self.top.get(r) for r in rids):
                continue                                                  # restored from disk
            self.search_role(role, rids, RESTART_PAGES_PER_ROLE if quick else None)
        for lst in self.top.values():
            for s in lst:
                cid, k = rec_id(s), name_key(s)
                if cid and k:
                    self.cluster[cid] = k
        self.save()

    # ---------------------------------------------------------- profiles
    def compact(self, cid, p, summary):
        return {
            "cid": cid,
            "assess": parse_score100(field(p, *F_ASSESS)),
            "notice": parse_days(field(p, *F_NOTICE)),
            "ctc": parse_lakh(field(p, *F_CTC)),
            "exp": parse_years(field(p, *F_EXP)),
            "skills": sorted(skill_set(field(p, "skills", default=field(summary, "skills")))),
            "notes": notes_text(p)[:600],
            "claimed": truthy(field(p, *F_CLAIMED)),
            "risk": heuristic_risk(p, summary),
            "email": str(field(p, "email", "phone", default="")).lower(),
        }

    def fetch_profiles(self, cids):
        """[(cid, raw profile)]: one batch call when >= 30 ids (60 cr vs 2 each), else singles."""
        out = []
        if len(cids) >= 30:
            raw = None
            for attempt in range(2):
                try:
                    raw = self.arena.batch(cids[:50])
                    break
                except (Exhausted, WrongPhase):
                    raise
                except Exception as e:
                    log(f"batch of {len(cids)} failed (attempt {attempt + 1}): {e}")
            if raw is None:
                return out
            for p in as_items(raw, "profiles", "candidates", "results", "items", "data"):
                p = unwrap(p, "candidate", "profile")
                if rec_id(p):
                    out.append((rec_id(p), p))
            return out
        for c in cids:
            try:
                p = unwrap(self.arena.candidate(c), "candidate", "profile")
            except (Exhausted, WrongPhase):
                raise
            except Exception as e:
                log(f"profile {c} failed: {e}")
                continue
            if p:
                out.append((rec_id(p) or c, p))
        return out

    def buy_profiles(self, cids, summaries):
        cids = [c for c in dict.fromkeys(cids) if c not in self.profiles]
        for i in range(0, len(cids), 50):
            chunk = cids[i:i + 50]
            items = self.fetch_profiles(chunk)
            self.decide("batch" if len(chunk) >= 30 else "profiles", n=len(chunk), got=len(items),
                        why="batch is 1.2 cr/profile" if len(chunk) >= 30 else "fewer than 30, single calls cheaper")
            for cid, p in items:
                self.profiles[cid] = self.compact(cid, p, summaries.get(cid, {}))
                if self.profiles[cid]["email"]:          # stronger duplicate key when present
                    self.cluster.setdefault(cid, "e:" + self.profiles[cid]["email"])

    def recon_profiles(self):
        summaries, wanted = {}, []
        for rid, lst in self.top.items():
            n = math.ceil(PROFILE_MULT * max(1, self.open_slots(rid)))
            seen_clusters = set()
            for s in lst:
                cid = rec_id(s)
                if not cid:
                    continue
                k = self.cluster.get(cid)
                if k and k in seen_clusters:
                    continue                                              # one id per person
                seen_clusters.add(k or cid)
                summaries[cid] = s
                wanted.append(cid)
                if len(seen_clusters) >= n:
                    break
        log(f"buying {len(set(wanted))} profiles")
        self.buy_profiles(wanted, summaries)
        self.save()

    # ---------------------------------------------------------- LLM for grey-zone notes
    @staticmethod
    def llm_text(res):
        t = field(res, "completion", "text", "content", "output", "response", "answer")
        choices = field(res, "choices")
        if t is None and isinstance(choices, list) and choices and isinstance(choices[0], dict):
            t = field(choices[0], "text") or field(field(choices[0], "message", default={}), "content")
        return str(t or "")

    @staticmethod
    def llm_tokens_used(res):
        t = field(res, "tokens", "tokens_used", "total_tokens", "usage")
        if isinstance(t, dict):
            t = field(t, "total_tokens", "total")
        return int(safe_num(t, 1500))

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
            except (Exhausted, WrongPhase):
                raise
            except Exception as e:
                log(f"LLM unavailable ({e}); falling back to heuristics")
                return
            self.llm_tokens += self.llm_tokens_used(res)
            m = re.search(r"\{.*\}", self.llm_text(res), re.S)
            try:
                scores = json.loads(m.group()) if m else {}
            except ValueError:
                scores = {}
            for p in chunk:
                v = scores.get(p["cid"]) if isinstance(scores, dict) else None
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    self.llm_risk[p["cid"]] = min(1.0, max(0.0, float(v)))
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
        if a is None:
            a = p["assess"]
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
        if rid not in self.reqs:
            return []
        out = []
        offer_cost = COST["offer"] * PENALTY_FACTOR
        for cid in list(self.profiles):
            if not self.available(cid) or f"{cid}|{rid}" in self.bad_pairs:
                continue
            ev, p_ok = self.value(cid, rid)
            if p_ok >= P_OK_MIN and ev > offer_cost:
                out.append((ev, cid))
        out.sort(reverse=True)
        return out

    # ---------------------------------------------------------- assessments
    def worth_assessing(self, cid, rid):
        """Value of information: P(bad) x slot value vs 25 x PF."""
        if cid in self.assessed or rid not in self.reqs:
            return False
        return self.p_bad(cid) * self.points(rid) > COST["assess"] * PENALTY_FACTOR

    def assess(self, cid, why):
        try:
            res = self.arena.assess(cid)
        except (Exhausted, WrongPhase):
            raise
        except Exception as e:
            log(f"assess {cid} failed: {e}")
            return None
        score, ok = assessment_verdict(unwrap(res, "assessment", "result"))
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
        """Offer cid for rid. Exhausted/WrongPhase propagate; any other failure returns False."""
        cost = COST["offer_closing" if closing else "offer"]
        with self.lock:
            if cid in self.pending or not self.available(cid) or self.open_slots(rid) <= 0:
                return False
            self.held[cid] = rid                                 # reserve locally before the call
            self.pending.add(cid)
        try:
            accepted, already_yours, reason = offer_outcome(self.arena.offer(cid, rid))
        except (Exhausted, WrongPhase):
            with self.lock:
                self.held.pop(cid, None)
                self.pending.discard(cid)
            raise
        except Exception as e:                                   # rejection may arrive as an HTTP error
            accepted, already_yours, reason = False, False, offer_reason(str(e))
            if reason is None:
                with self.lock:
                    self.held.pop(cid, None)
                    self.pending.discard(cid)
                log(f"offer {cid}->{rid} error: {e}")
                return False
        with self.lock:
            self.pending.discard(cid)
            k = self.cluster.get(cid)
            if accepted:
                self.released.discard(cid)
                if rid in self.filled_srv:
                    self.filled_srv[rid] += 1
                if k:
                    self.dead.add(k)                             # never offer this person again
                self.decide("offer", cid=cid, req=rid, cost=cost, accepted=True, already_yours=already_yours,
                            why=f"ev={self.value(cid, rid)[0]:.1f} > {cost}x{PENALTY_FACTOR}")
                log(f"SIGNED {cid} -> {rid}")
                return True
            self.held.pop(cid, None)
            if reason in ("already_signed", "same_person_already_signed"):
                if k:
                    self.dead.add(k)
                self.profiles[cid]["claimed"] = True
            elif reason == "requisition_full":
                self.filled_srv[rid] = self.headcount(self.reqs[rid])
            elif reason == "role_mismatch":
                self.bad_pairs.add(f"{cid}|{rid}")
            self.decide("offer", cid=cid, req=rid, cost=cost, accepted=False, reason=reason)
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

    def _burst_worker(self, rid):
        """Thread body: hand Exhausted/WrongPhase to the main thread instead of dying silently."""
        try:
            self.fill_req(rid)
        except (Exhausted, WrongPhase) as e:
            with self.lock:
                if self.burst_error is None or isinstance(e, Exhausted):
                    self.burst_error = e
        except Exception:
            log(f"burst worker {rid} error (continuing):\n" + traceback.format_exc())

    def opening_burst(self):
        """11:30: one worker per requisition, all firing at once (throttle keeps us legal)."""
        self.burst_error = None
        workers = [threading.Thread(target=self._burst_worker, args=(rid,), daemon=True) for rid in self.reqs]
        for w in workers:
            w.start()
        for w in workers:
            w.join()
        self.save()
        log(f"burst done: holding {len(self.held)}")
        if self.burst_error is not None:
            raise self.burst_error

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
        items = self.fetch_profiles(ids)
        for cid, p in items:
            if cid in self.profiles:
                self.profiles[cid]["claimed"] = truthy(field(p, *F_CLAIMED))
        self.decide("refresh", req=rid, n=len(ids), why="avoid paying 10 for already_signed")

    def release(self, cid, why):
        """Release a hold; the slot comes back, the 5 credits do not."""
        try:
            self.arena.release(cid)
        except (Exhausted, WrongPhase):
            raise
        except Exception as e:
            log(f"release {cid} failed: {e}")
            return False
        with self.lock:
            rid = self.held.pop(cid, None)
            self.released.add(cid)
            if rid in self.filled_srv:
                self.filled_srv[rid] = max(0, self.filled_srv[rid] - 1)
        self.decide("release", cid=cid, req=rid, why=why)
        return True

    def verify_holds(self):
        for cid, rid in list(self.held.items()):
            if cid in self.pending or cid not in self.profiles or rid not in self.reqs:
                continue
            if self.worth_assessing(cid, rid):
                ok = self.assess(cid, why="post-sign check, no race now")
                if ok is False or self.value(cid, rid)[0] == 0:
                    self.release(cid, why="failed verified assessment")

    def maybe_upgrade(self, rid):
        if self.open_slots(rid) > 0:
            return
        mine = [(self.value(c, rid)[0], c) for c, r in list(self.held.items()) if r == rid and c in self.profiles]
        best = self.ranked(rid)[:1]
        if not mine or not best:
            return
        worst_ev, worst = min(mine)
        gain = best[0][0] - worst_ev - (COST["release"] + COST["offer"]) * PENALTY_FACTOR
        if gain > UPGRADE_MIN_GAIN and self.release(worst, why=f"upgrade gain {gain:.1f} pts"):
            self.try_offer(best[0][1], rid)

    def expand_pool(self, rid):
        """Queue ran dry but slots remain: buy the next profiles from the kept summaries."""
        summaries = {rec_id(s): s for s in self.top.get(rid, []) if rec_id(s)}
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
        pr = field(m, "price_pressure", "pressure", default={})
        if isinstance(pr, list):
            pr = {rec_id(x, REQ_KEYS): safe_num(field(x, "pressure", "signings", "count", "value"), 0)
                  for x in pr if isinstance(x, dict)}
        self.pressure = pr if isinstance(pr, dict) else {}
        log(f"market: rank={field(m, 'rank', 'your_rank')} leader={field(m, 'leader_score')}")

    def market_loop(self):
        """Trade until the phase leaves market/closing. Exhausted and WrongPhase go up to run()."""
        while True:
            ph = self.phase()
            if ph == "unknown":                  # no phase field: probe for closed, else act as market
                if self.probe_phase() == "closed":
                    return
                ph = "market"                    # an offer 409 then means offers are not open yet
            if ph not in ("market", "closing"):
                return
            closing = ph == "closing"
            try:
                if not self.started_market and not closing:
                    self.opening_burst()                 # raises WrongPhase if offers are still locked
                    self.started_market = True
                self.refresh_reqs()
                open_reqs = [r for r in self.reqs if self.open_slots(r) > 0]
                if open_reqs and not closing:
                    self.poll_market()
                open_reqs.sort(key=lambda r: -safe_num(self.pressure.get(r), 0))
                for rid in open_reqs:
                    if not closing:
                        self.refresh_stale(rid)
                    self.fill_req(rid, closing)
                    if self.open_slots(rid) > 0 and not self.ranked(rid) and not closing:
                        self.expand_pool(rid)
                if not closing:
                    self.verify_holds()
                    for rid in list(self.reqs):
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
    def recon(self, quick):
        """Build the offer queue. quick = restarted mid-market without a state file: shallow
        search + profiles only, so we trade again within a minute or two."""
        if quick and self.profiles:
            log(f"resuming mid-market from state file: {len(self.profiles)} profiles, skipping recon")
            return
        steps = [lambda: self.recon_search(quick), self.recon_profiles]
        if not quick:
            steps += [self.llm_screen, self.recon_assess]
        for step in steps:
            try:
                step()
            except (Exhausted, WrongPhase):
                raise
            except Exception:
                log("recon step failed (continuing):\n" + traceback.format_exc())
        log(f"recon done (quick={quick}): {len(self.profiles)} profiles, {len(self.assessed)} assessed, "
            f"credits left {self.arena.credits_remaining}")

    def run(self):
        """Supervisor: only Exhausted or the arena closing ends the run; everything else retries."""
        log(f"PF={PENALTY_FACTOR} RPS={RPS} PROFILE_MULT={PROFILE_MULT}")
        saved_req_ids = set(self.reqs)
        seen_open = False
        try:
            while True:
                try:
                    ph = self.phase()
                    if ph == "unknown":
                        ph = "closed" if self.probe_phase() == "closed" else \
                            ("market" if self.recon_done else "recon")
                    if ph == "closed":
                        if seen_open or self.arena_finished():
                            log("arena closed")
                            return
                        time.sleep(5)
                        continue
                    seen_open = True
                    if not self.setup_done:
                        self.refresh_reqs()
                        self.check_state(saved_req_ids)
                        log(f"phase {ph}: {len(self.reqs)} requisitions, {self.arena.credits_remaining} credits")
                        self.tune_knobs()
                        self.setup_done = True
                    if not self.recon_done:
                        self.recon(quick=ph in ("market", "closing"))
                        self.recon_done = True
                    if ph == "recon":
                        time.sleep(1)                                     # poll for the market flip
                        continue
                    self.market_loop()
                except Exhausted:
                    log("out of credits: holding what we have")
                    return
                except WrongPhase as e:
                    log(f"409 from the arena ({str(e)[:120]}); re-reading the phase")
                    time.sleep(5 if self.last_phase_warn else 1)
                except Exception:
                    log("run error (continuing):\n" + traceback.format_exc())
                    time.sleep(LOOP_SLEEP_S)
        finally:
            self.save()
            try:
                log(f"FINAL ledger: {json.dumps(self.arena.ledger(), default=str)[:500]}")
            except Exception:
                pass


if __name__ == "__main__":
    Agent(ThrottledArena()).run()
