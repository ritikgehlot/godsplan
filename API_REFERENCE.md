# Battle Arena - API reference (finalists)

Base URL and your team key are handed out at the finale. Send your key on every request:
`X-Arena-Key: arena_xxx`. Every response carries `X-Credits-Remaining`. The full machine-readable
spec is in `openapi.json`.

| Endpoint | Returns | Cost |
|---|---|---|
| `GET /requisitions` | open roles: skills wanted, headcount, bar (min assessment, max notice, max expected CTC), your filled/remaining slots | free |
| `GET /search?q=&role=&city=&page=&size=` | up to 100 summary records (name, role, city, experience, skills) | 1 |
| `GET /candidate/{id}` | one full profile (incl. notes, assessment, notice, CTC, `claimed`) | 2 |
| `POST /candidates/batch` `{"ids":[...]}` | 1 to 50 full profiles | 60 |
| `GET /assess/{id}` | verified assessment + reference check | 25 |
| `POST /offer` `{"candidate_id","req_id"}` | sign a candidate (exclusive) | 10 (20 in closing) |
| `DELETE /offer/{id}` | release a signing; slot returns, credits do not | 5 |
| `GET /market` | signings per requisition, price pressure (signings in the last 10 min), your rank, leader score | 2 |
| `GET /ledger` | your credits, signings, points, score, phase | free |
| `POST /reason` `{"prompt","max_tokens"}` | LLM completion | 1 per 1K tokens |

**Requisitions.** Each requisition has a headcount **for your team**: every team can sign up to
that many people into it. Candidates are exclusive across teams: whoever signs a person first has
them until they release them.

**Offers can be rejected**, and you still pay: `already_signed` (this person is held by a team,
possibly under another id), `requisition_full` (you have filled that requisition),
`role_mismatch`, `same_person_already_signed` (you already hold this person under another id).
Check before you spend. Repeating an offer you already hold (same candidate, same requisition)
returns `accepted: true, already_yours: true`, so a retry after a network timeout is safe (it is
still charged).

**Points and score.** `score = points of candidates you still hold − credits used × PENALTY FACTOR`.
The penalty factor is announced at kick-off. The points shown on `/ledger`, `/market` and the
leaderboard are **refreshed every 15 minutes** (`points_as_of` tells you when); credits are always
live. Once the arena closes everything is final and live.

**Phases:** `recon` (search, profiles, assessments; no offers) → `market` → `closing` (offers cost
double; `/market` is a snapshot refreshed once a minute) → `closed`. Outside recon/market/closing
every metered call returns `409`; `/requisitions` and `/ledger` always answer.

**Limits:** 50,000 credits per team; 10 requests/second per team, burst 30, counting every
endpoint including the free ones. Exceed it and you get `429 rate_limited` (the supplied client
backs off and retries; it costs nothing). Out of credits is `429 credits_exhausted`, and it is
final: a call that would take you below zero is refused and charges nothing.

**/reason:** at most 4,000 tokens per call, prompt and completion together, and at most 2 calls in
flight per team. You are charged for the tokens the model actually used (returned as `tokens`);
the estimate held while the call runs is settled afterwards. A failed model call is refunded.
Each team may use at most **450,000 model tokens** in total; after that, and whenever the endpoint
is disabled, `/reason` returns `503` and charges nothing.

**Errors:** `401` unknown key · `403` key revoked · `404` unknown candidate/requisition ·
`409` wrong phase · `413` body over 64 KB · `422` malformed request (`{"error":"bad_request"}`) ·
`429` rate limited or out of credits.

Start from `agent_template.py`. It runs through every phase, keeps a closing reserve and signs a few
people, badly.
