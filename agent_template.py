"""
Minimal Battle Arena agent - a running starting point, deliberately naive.

    export ARENA_URL=https://arena.example.com
    export ARENA_KEY=arena_xxx
    export PENALTY_FACTOR=0.05          # announced at kick-off
    python agent_template.py

What it does, phase by phase:
    closed    waits for the arena to open
    recon     one search page per requisition, keeps a shortlist (offers are locked)
    market    buys a profile for each shortlisted candidate, signs anyone who looks
              good enough, never spending below the closing reserve
    closing   spends the reserve on a few more signings (offers cost double now)
    closed    prints the ledger and exits

It handles running out of credits, empty searches, rejected offers and the rate limit
(the client retries 429s for you). It does NOT do any of the things that score well:
see "IDEAS" at the bottom. Beating it should be easy.
"""
import os, re, time
from arena_client import Arena, Exhausted, WrongPhase

# ---------------------------------------------------------------- knobs
PENALTY_FACTOR = float(os.environ.get("PENALTY_FACTOR", 0.05))   # set this at kick-off
CLOSING_RESERVE = 2000       # credits kept back until the closing phase
SIGN_PER_REQ = 3             # how many people to sign per requisition in the market phase
EXTRA_IN_CLOSING = 1         # how many more per requisition once the closing phase starts
POLL_SECONDS = 30            # how often to check the phase while waiting


def log(*msg):
    print(time.strftime("%H:%M:%S"), *msg, flush=True)


# ---------------------------------------------------------------- parsing (naive on purpose)
def first_number(text, default=0.0):
    """First number in a messy string: '71/100' -> 71, '45 days' -> 45, '' -> default."""
    m = re.search(r"\d+(\.\d+)?", str(text))
    return float(m.group()) if m else default


def skill_count(summary):
    """How many skills are listed. Ignores WHICH skills, and spellings like 'k8s' vs 'Kubernetes'."""
    return len([s for s in re.split(r"[,;|]", summary.get("skills", "")) if s.strip()])


def looks_good_enough(profile, req):
    """Reads the profile's own assessment and notice period at face value. No verification."""
    assessment = first_number(profile.get("assessment"), 0)
    notice = 0 if "immediate" in str(profile.get("notice_period", "")).lower() \
        else first_number(profile.get("notice_period"), 0)
    return assessment >= req["min_assessment"] and notice <= req["max_notice_days"]


# ---------------------------------------------------------------- phases
def current_phase(arena):
    """/ledger is free, so it is the cheap way to read the phase."""
    return arena.ledger()["phase"]


def wait_while(arena, phases):
    """Sleep until the arena leaves the given phase(s)."""
    while True:
        phase = current_phase(arena)
        if phase not in phases:
            return phase
        time.sleep(POLL_SECONDS)


def build_shortlist(arena, reqs):
    """Recon: one page of summaries per requisition, ranked by how many skills they list."""
    shortlist = {}
    for req in reqs.values():
        page = arena.search(role=req["role"], size=100)          # 1 credit
        results = page.get("results", [])
        if not results:
            log(f"{req['req_id']}: search came back empty")
            shortlist[req["req_id"]] = []
            continue
        ranked = sorted(results, key=skill_count, reverse=True)
        shortlist[req["req_id"]] = [c["candidate_id"] for c in ranked[:15]]
        log(f"{req['req_id']} {req['role']}: {len(results)} summaries, kept {len(shortlist[req['req_id']])}")
    return shortlist


def sign_some(arena, reqs, shortlist, per_req, reserve, signed):
    """Walk each shortlist: buy the profile, offer if it looks good. Stops at the reserve."""
    for req_id, ids in shortlist.items():
        req = reqs[req_id]
        while ids and signed.get(req_id, 0) < per_req:
            if arena.credits_remaining is not None and arena.credits_remaining <= reserve:
                log(f"reached the reserve ({reserve} credits), stopping for now")
                return
            cid = ids.pop(0)
            profile = arena.candidate(cid)                         # 2 credits
            if profile.get("claimed") or not looks_good_enough(profile, req):
                continue
            result = arena.offer(cid, req_id)                      # 10 credits (20 in closing)
            if result.get("accepted"):
                signed[req_id] = signed.get(req_id, 0) + 1
                log(f"signed {cid} for {req_id}")
            elif result.get("reason") == "requisition_full":
                log(f"{req_id} is full")
                break
            else:
                log(f"offer for {cid} rejected: {result.get('reason')}")


def main():
    arena = Arena()
    log(f"penalty factor {PENALTY_FACTOR}; waiting for the arena to open")
    signed = {}
    try:
        phase = wait_while(arena, ("closed",))
        reqs = {r["req_id"]: r for r in arena.requisitions()}      # free
        log(f"phase {phase}: {len(reqs)} requisitions, {arena.credits_remaining} credits")

        shortlist = build_shortlist(arena, reqs)

        phase = wait_while(arena, ("recon",))                      # offers are locked in recon
        if phase == "market":
            sign_some(arena, reqs, shortlist, SIGN_PER_REQ, CLOSING_RESERVE, signed)
            log("done for the market phase; idling until closing")
            phase = wait_while(arena, ("market",))
        if phase == "closing":
            sign_some(arena, reqs, shortlist, SIGN_PER_REQ + EXTRA_IN_CLOSING, 0, signed)
            wait_while(arena, ("closing",))
    except Exhausted:
        log("out of credits - nothing more can be bought, only free calls work now")
    except WrongPhase as e:
        log(f"call refused in this phase: {e}")
    led = arena.ledger()
    log(f"final: signed {led['signed']}, credits used {led['credits_used']}, "
        f"points {led['points']}, score {led['score']}")


if __name__ == "__main__":
    main()

# IDEAS - what this agent ignores, roughly in order of value:
#   * requisitions list the skills they want; normalise spellings (JS, k8s, Postgres ...) and match them
#   * read the whole pool's summaries during recon - a search page of 100 costs 1 credit
#   * batch profiles (50 for 60 credits) instead of 2 credits each
#   * the requisition bar has a salary budget too; the profile carries expected CTC
#   * a profile's assessment is unverified (and sometimes missing); /assess is the verified one
#   * fabricated profiles exist - which ones would you check before signing?
#   * recruiter notes carry signal
#   * the same person can appear twice under different ids
#   * sign at 11:30 sharp: the best names go first
#   * decide spend from PENALTY_FACTOR instead of fixed numbers
