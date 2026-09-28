# Claude Code brief: make our Battle Arena agent as strong as possible (LOCAL TESTING ONLY)

You are improving `agent.py`, our autonomous agent for the Innov8 4.0 Battle Arena finale
(Eightfold.ai x ARIES, IIT Delhi). The arena is LIVE today. Work fast, commit often, and never
leave the repo in a broken state: after every milestone, `agent.py` must be deployable as-is.

---------------------------------------------------------------------------------------------
## 0. ABSOLUTE RULES (breaking any one disqualifies our team)

1. **Never call the live arena.** Do not ask for, read, print, or store our team key.
   Every command that runs the agent must set, in the same command:
   `ARENA_URL=http://127.0.0.1:<port> ARENA_KEY=test`.
   Add a guard to every test/sim runner that aborts if `ARENA_URL` is not 127.0.0.1/localhost.
2. **No network access** from the agent other than `ARENA_URL`. No new dependencies.
3. **No human steering.** No reading commands from files or URLs at runtime, no hard-coded
   candidate IDs, no thresholds tuned from watching the live leaderboard or market. Only
   general logic and bug fixes. (Aligning JSON field names to real sample responses is a bug fix.)
4. **Do not modify `arena_client.py`** (official). Subclassing it inside agent.py is fine.
5. **Deployable shape:** entry point `agent.py`, may import only stdlib, `arena_client`,
   numpy, pandas, scikit-learn, requests, orjson. Keep the agent in `agent.py` (single file)
   because we may not be able to upload more than agent.py + arena_client.py.
6. **Never hard-code the key.** It is injected as `ARENA_KEY`.
7. **Honesty:** never report a number you did not produce by running code. If something is
   untested or assumed, say so plainly.

---------------------------------------------------------------------------------------------
## 1. THE GAME (facts from the organisers' case file and API reference)

- Pool: 1,000,000 candidate profiles shared by 40 teams. Candidates are exclusive: whoever
  signs a person first holds them until released. Same person may exist under several IDs.
  Fabricated profiles exist; signing one scores zero and blocks the slot until released.
- Requisitions: skills wanted, headcount (per team: every team can fill that many), bar
  (min assessment, max notice days, max expected CTC), points on offer, your filled/remaining.
- Score = points of candidates held at the final whistle - credits_used x PENALTY_FACTOR.
  **PENALTY_FACTOR = 0.05.** Tie-breaks: fewer credits used, then earlier time of final score.
- Final result: 70% arena score, 10% edge cases (outliers, empty results, API errors, rate
  limits, closing minutes), 10% logic/architecture, 10% readability.
- Phases (IST): recon 10:30-11:30 (search/profiles/assess live, offers locked) -> market
  11:30-15:30 -> closing 15:30-16:30 (offers cost 20, /market is a snapshot refreshed once a
  minute) -> closed. Outside recon/market/closing, metered calls return 409;
  /requisitions and /ledger always answer. Phase is read from `GET /ledger` (free).
- Costs (credits): /requisitions free; /search 1 per page of up to 100 summaries (name, role,
  city, experience, skills); /candidate/{id} 2 (full profile: notes, self-reported assessment,
  notice, CTC, `claimed`); /candidates/batch 60 for 1-50 profiles; /assess/{id} 25 (verified
  assessment + reference check); /offer 10 (20 in closing); DELETE /offer/{id} 5 (slot returns,
  credits don't); /market 2 (signings per req, price pressure, your rank, leader score);
  /ledger free; /reason 1 per 1K tokens.
- Offers can be rejected AND are still charged: `already_signed`, `requisition_full`,
  `role_mismatch`, `same_person_already_signed`. Re-offering a held (candidate, req) returns
  `accepted: true, already_yours: true` (charged).
- Points on /ledger, /market and the leaderboard refresh every 15 min (`points_as_of`);
  credits are live.
- Limits: 50,000 credits, hard; a call that would go below zero is refused and not charged;
  out of credits = `429 credits_exhausted` (final). 10 req/s per team, burst 30, free calls
  count; excess = `429 rate_limited` (client retries). /reason: <=4,000 tokens per call,
  <=2 in flight, <=450,000 tokens total, 503 when disabled or exhausted, failed calls refunded.
  Errors: 401, 403, 404, 409, 413 (body > 64 KB), 422, 429.
- Runtime: one container, 0.5 vCPU, 1 GB RAM, ARM64, Python 3.11. If the agent crashes it is
  restarted twice at the same credit balance; a third crash ends our run. A redeploy kills the
  process and restarts it from scratch: in-memory state lost, credits not refunded. Disk may or
  may not survive; design for both.

---------------------------------------------------------------------------------------------
## 2. FILES IN THIS FOLDER

- `agent.py`: current agent (v1). Read it fully first. Its design: PF-driven value-of-information
  rule (buy only if expected points > credits x PF), wide cheap search, batched profiles,
  heuristic + LLM fraud screening, selective /assess, opening burst at market open, then a
  maintenance loop (refresh claimed, verify holds, upgrades), checkpoint to disk,
  `decisions.jsonl` audit log, `SAMPLE` log line per endpoint, `TUNE` knob selection.
- `arena_client.py`: official client (do not modify).
- `agent_template.py`: organisers' naive baseline.
- `mock_arena.py`: a small mock I used for crash tests. Its data and scoring are made up.
- `API_REFERENCE.md`, `openapi.json`: official spec (response schemas are EMPTY, so field
  names in agent.py are educated guesses).
- `STRATEGY_NOTE.md`: our one-page submission note.

### Real response samples from the live arena (field names ONLY)
If the section below is filled, it is ground truth for field names and value formats.
If it still says PASTE, keep all defensive fallbacks and list every assumed field name
in your final report.

```
PASTE THE SAMPLE LINES FROM OUR AGENT'S LOG HERE (never the key)
```

---------------------------------------------------------------------------------------------
## 3. WORK PLAN (strict priority order; commit + tag after each milestone)

Run `git init` if needed. Tag milestones `m1`, `m2`, ... so we can deploy any of them.

### M1 (fastest, most important): real-API alignment + crash safety
1. If samples are present: align every key the agent reads (all `field(...)` calls, `compact`,
   `parse_bar`, `heuristic_risk`, `assessment_verdict`, `try_offer`, `phase`, `refresh_reqs`,
   `buy_profiles`, `search_role`, `llm_screen`, `poll_market`) to the real names. Put the real
   name first; keep old names as fallbacks. Handle list vs dict response shapes.
2. Unit-test the parsers (`parse_score100`, `parse_days`, `parse_lakh`, `parse_years`,
   `skill_set`, `name_key`, `assessment_verdict`) on real sample values plus nasty cases:
   None, "", "N/A", "71/100", "7.1/10", "0.71", "71%", "immediate", "2 months", "3 weeks",
   "serving notice", "12 LPA", "12,00,000", "1.2 Cr", "80k/month", "k8s", "Pyhton", "Node.js",
   "Sharma, Rahul" vs "rahul  sharma".
3. Slot accounting must trust the server: use filled/remaining from `/requisitions` and signings
   from `/ledger` when available, so a restart or redeploy never wastes offers on
   `requisition_full` or re-offers people we already hold.
4. Restart/redeploy safety: resume from the state file if present; if absent (fresh disk)
   mid-market, rebuild holdings from server truth and run a short targeted recon, then
   continue. Test both cases.
5. Audit every place an exception could escape, including inside `opening_burst` threads.
   Only `Exhausted` (out of credits) and the arena closing may end the run.

### M2: a realistic local simulator (for testing only, clearly labelled synthetic)
Build `sim/arena_sim.py` (you may replace mock_arena.py). It must enforce everything in
section 1: phases (time-compressed, configurable), all costs incl. closing double, 50k credits,
token-bucket rate limit (10 rps, burst 30, free calls included), 409 in wrong phase,
per-team headcount, exclusivity, same-person detection across IDs, rejected-offer charging,
`already_yours`, 15-minute (scaled) points refresh on /ledger, /reason with a deterministic fake
model that is sometimes 503.
- Messy pool: at least 100k candidates for normal runs, plus a separate scale test that pages
  through 1M summaries to check the agent stays under ~700 MB RSS and keeps CPU per summary low
  on a throttled core. Skill aliases/typos, mixed assessment/notice/CTC formats, missing
  fields, recruiter notes carrying real signal (red flags, "accepted another offer", strong
  references), duplicates and near-duplicates under different IDs, ~3-8% fabricated profiles
  with inflated self-assessments that fail /assess.
- Hidden points function: implement at least 3 plausible variants (e.g. depends on verified
  assessment margin, skill coverage, notice, CTC slack; fabricated/ineligible = 0). Our agent
  must not overfit any single one; report results per variant.
- Rivals: 39 bots with mixed strategies (template-like naive; greedy on self-reported
  assessment; fast snipers that fire at market open; slow late signers; occasional releases).
- Fault injection: random 429 rate_limited, 5xx, timeouts, empty pages, malformed or missing
  fields, bare-list vs dict responses, credits running out mid-run.
- Runner: `python sim/run.py --agent agent.py --seeds 5 --variant all` prints per-seed:
  score, points, credits used, signings, fabricated signed, rejected offers by reason,
  assessments, LLM tokens, crashes. Also runs `agent_template.py` and the tagged `m1` build
  as baselines.

### M3: strategy improvements, each kept ONLY if it wins in the simulator
For each idea: implement behind a named knob, A/B against the previous best over >= 5 seeds
x all points variants, keep it only if the mean score improves and nothing gets less robust.
Report the actual numbers. Candidate ideas, roughly by expected value:
1. **Opening burst readiness:** finish recon work before the market opens, poll the phase often
   enough near the flip without breaking 10 rps, fire immediately, interleave requisitions so
   each gets its best pick first.
2. **Contention-aware ordering:** consensus top names (high self-assessment, perfect summary)
   are contested by many rivals; rank by expected points x estimated P(we win it), learning
   P(win) from our own `already_signed` rate as the market runs.
3. **Online fraud calibration:** as /assess results arrive, fit a small logistic regression
   (scikit-learn) from profile features to "failed verification" and use it to replace the
   hand-set heuristic weights. Fall back to heuristics until enough labels exist.
4. **Assessment policy at PF 0.05:** one assessment costs 1.25 points. Test assessing every
   candidate before offering vs value-of-information gating vs post-sign verification.
5. **Search depth and `q` usage:** a search page costs 0.05 points at this PF. Test deeper
   reading, and targeted queries with the `q` parameter using required skills. Keep `q` behind
   a knob that is OFF unless the samples show how it behaves.
6. **Stronger duplicate clustering:** email/phone/link fields if present, fuzzy name + same
   experience/city, so we never pay for `same_person_already_signed` or double-buy profiles.
7. **Upgrades and releases:** release the weakest hold only when the replacement's expected gain
   beats release + offer cost and the replacement is verified; never leave a slot empty near
   the close.
8. **Closing hour and final minutes:** keep enough credits to fill any empty slot at 20 per
   offer; stop all non-essential spend (e.g. /market polls) once every slot holds a verified
   candidate; never release anything in the final minutes.
9. **Learn points from the ledger** if the ledger or market exposes per-signing points (check
   samples): refit the quality weights from observed points every refresh.
10. **Spend discipline:** every recurring paid call (/market, refreshes) must justify itself;
    credits are the first tie-breaker.

### M4: readability and judging pack
- Clear module docstring (architecture + decision rules), one config block at the top with a
  comment per knob, short docstrings on every function, no dead code, descriptive names.
- `decisions.jsonl` must explain every paid action in plain words (the judges will ask why the
  agent bought what it bought).
- Update `STRATEGY_NOTE.md` (max one page): what we buy, what we sign, when we stop, what we
  tuned for PF = 0.05, what we'd do with two more hours. AI declaration: "Claude and
  Claude Code (Anthropic) were used to write and test the agent."

---------------------------------------------------------------------------------------------
## 4. DEFINITION OF DONE (for every milestone)

- `python -m py_compile agent.py` passes; all unit tests pass.
- The simulator completes all phases on every seed with zero tracebacks and zero crashes.
- The agent never exceeds 10 rps in the simulator's rate-limit log.
- No code path calls anything but `ARENA_URL`; key never logged.

## 5. FINAL REPORT (print at the end)

1. Milestone tags and what each contains (one line per change, with the reason).
2. A results table from actual simulator runs: template vs m1 vs final, mean and min score
   per points variant, credits used, fabricated signed, rejected offers. Say clearly these
   are synthetic results and may not transfer to the live arena.
3. Every field name that is still an assumption.
4. Known risks, plainly.
5. Exact deploy instructions: which files to upload, which env vars (`ARENA_URL`, `ARENA_KEY`
   are injected; `PENALTY_FACTOR=0.05`).
