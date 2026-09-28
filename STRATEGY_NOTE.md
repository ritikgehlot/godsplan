# god'splan — Battle Arena strategy note

**Core rule.** Every paid action is taken only if its expected points exceed `credits × PENALTY_FACTOR`. PF is an env var, so the whole information/spend trade-off moved with one number at kick-off.

**What we buy.**
- *Search (1 cr / 100 summaries):* role-filtered pages, at least `MIN_PAGES_PER_ROLE` deep, then stop after `SEARCH_PATIENCE` pages that add nothing to any requisition's top-K. Summary score = normalised skill coverage (alias map + typo-tolerant matching: k8s, Postgres, Pyhton) and experience fit.
- *Profiles:* `PROFILE_MULT` × open slots per requisition, one id per person (duplicate clusters by name+role, email when present). Always batched when ≥30 ids (1.2 cr vs 2).
- *LLM (/reason):* only for grey-zone notes (heuristic risk 0.1–0.6), 8 candidates per call, returns a fabrication/unavailability probability. Falls back to heuristics on 503.
- *Assessments (25 cr):* only when `P(bad) × slot value > 25 × PF`, and only near the top of a queue, so verification is spent where an offer actually hangs on it.

**What we sign.** Expected value = P(ok) × requisition points × fit quality (skill coverage, assessment margin over bar, notice and CTC slack). Hard bar checks on assessment, notice, CTC. Sign only if P(ok) ≥ `P_OK_MIN` and EV > offer cost × PF (cost doubles in closing).

**Market play.** Queue is built in recon; at market open one worker per requisition fires offers immediately, throttled below the rate limit, falling down the queue on `already_signed`. Afterwards: refresh claimed status in bulk before offering (cheaper than a rejected 10-cr offer), verify risky holds post-signing and release failures, upgrade the weakest hold when the gain beats release+offer cost.

**When we stop.** When no action clears its PF-adjusted cost, the agent idles on free calls. Credits not spent are score.

**Robustness.** Throttle under 10 rps; client retries 429/5xx; 409 and credit exhaustion end cleanly; state checkpointed to disk so a crash restart does not re-buy; every paid decision logged to `decisions.jsonl` with its reason.

**PF tuning at kick-off.** PF = 0.05 (announced at kick-off). At this PF the full 50,000-credit budget costs 2,500 points, one assessment 1.25 points, one offer 0.5. Changed: rather than guessing knob values, the agent reads points per hire from `/requisitions` at startup and computes what one hire is worth in credits (points ÷ PF). Above 1,000 it explores more (8× profiles and 3× assessments per slot, 150+ search pages per role); below 200 it runs frugal; otherwise the defaults. The chosen tier is logged as `TUNE` and in `decisions.jsonl`.

**With two more hours.** Learn the true points function from `/ledger` point deltas (refreshed every 15 min) and refit the quality weights live; calibrate fraud-heuristic weights against `/assess` outcomes.

**AI assistance declared.** Claude (Anthropic) was used to help write and test the agent.
