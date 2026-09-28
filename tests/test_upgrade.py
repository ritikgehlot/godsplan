"""Unit tests for the verified upgrade pass (rules 5, 6, 7 and FAIL-first ordering). No network.

    python -m unittest discover -s tests -v
"""
import os, sys, tempfile, unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import agent as A  # noqa: E402


class FakeArena:
    """Scripted arena: verified scores, claimed set, rejected offers. Records every paid call."""
    base, credits_remaining = "http://127.0.0.1:0", 50000

    def __init__(self, verified, claimed=(), reject=()):
        self.verified, self.claimed, self.reject = verified, set(claimed), set(reject)
        self.calls = []

    def assess(self, cid):
        self.calls.append(("assess", cid))
        score, ok = self.verified[cid]
        return {"verified_assessment": score, "reference_check": "passed" if ok else "failed"}

    def candidate(self, cid):
        self.calls.append(("candidate", cid))
        return {"candidate_id": cid, "claimed": cid in self.claimed}

    def offer(self, cid, rid):
        self.calls.append(("offer", cid))
        return {"accepted": False, "reason": "already_signed"} if cid in self.reject else {"accepted": True}

    def release(self, cid):
        self.calls.append(("release", cid))
        return {"released": True}

    def released(self):
        return [c for k, c in self.calls if k == "release"]


def profile(cid, self_assess):
    return {"cid": cid, "claimed": False, "assess": self_assess, "notice": 0, "ctc": 1, "skills": [],
            "risk": 0.05, "notes": "", "exp": 3, "email": ""}


class UpgradeTests(unittest.TestCase):
    def make(self, holds, cands, verified, **kw):
        tmp = tempfile.mkdtemp()
        A.STATE_PATH, A.DECISIONS_PATH = os.path.join(tmp, "s.json"), os.path.join(tmp, "d.jsonl")
        A.NO_RELEASE_AFTER = "23:59"
        ag = A.Agent(FakeArena(verified, **kw))
        ag.reqs = {"R1": {"req_id": "R1", "headcount": len(holds), "min_assessment": 60}}
        ag.bars = {"R1": A.Agent.parse_bar(ag.reqs["R1"])}
        ag.filled_srv = {"R1": len(holds)}
        for c in holds:
            ag.held[c] = "R1"
            ag.profiles[c] = profile(c, 90)
        for c, sa in cands.items():
            ag.profiles[c] = profile(c, sa)
        return ag

    def test_rule5_no_release_without_two_ready(self):
        ag = self.make(["H1", "H2"], {"C1": 90, "C2": 85},
                       {"H1": (40, False), "H2": (80, True), "C1": (85, True), "C2": (50, True)})
        ag.upgrade_pass(closing=False)
        self.assertEqual(ag.arena.released(), [])                  # only one verified PASS replacement
        self.assertEqual(set(ag.held), {"H1", "H2"})

    def test_rule5_releases_with_two_ready_and_refills(self):
        ag = self.make(["H1", "H2"], {"C1": 90, "C2": 85},
                       {"H1": (40, False), "H2": (80, True), "C1": (85, True), "C2": (75, True)})
        ag.upgrade_pass(closing=False)
        self.assertEqual(ag.arena.released(), ["H1"])
        self.assertIn("C1", ag.held)                                # best verified replacement signed
        self.assertEqual(ag.open_slots("R1"), 0)

    def test_rule6_pass_hold_needs_margin(self):
        verified = {"H1": (70, True), "H2": (80, True), "C1": (75, True), "C2": (78, True)}
        ag = self.make(["H1", "H2"], {"C1": 95, "C2": 95}, verified)
        ag.upgrade_pass(closing=False)                              # gains 5 and 8 < UPGRADE_MARGIN 10
        self.assertEqual(ag.arena.released(), [])
        verified.update(C1=(85, True), C2=(90, True))
        ag = self.make(["H1", "H2"], {"C1": 95, "C2": 95}, verified)
        ag.upgrade_pass(closing=False)                              # 90 - 70 = 20 >= 10: swap worst
        self.assertEqual(ag.arena.released()[0], "H1")
        self.assertIn("C2", ag.held)

    def test_rule7_no_release_after_cutoff(self):
        ag = self.make(["H1", "H2"], {"C1": 90, "C2": 85},
                       {"H1": (40, False), "H2": (80, True), "C1": (85, True), "C2": (75, True)})
        A.NO_RELEASE_AFTER = "00:00"
        ag.upgrade_pass(closing=True)
        self.assertEqual(ag.arena.released(), [])
        self.assertEqual([k for k, _ in ag.arena.calls], [])        # no spend at all once locked

    def test_fail_first_then_lowest_margin(self):
        ag = self.make(["H1", "H2"], {"C1": 95, "C2": 95, "C3": 95},
                       {"H1": (61, True), "H2": (59, True), "C1": (95, True), "C2": (92, True), "C3": (90, True)})
        ag.upgrade_pass(closing=False)
        self.assertEqual(ag.arena.released()[0], "H2")              # below bar = FAIL, goes first

    def test_fabricated_before_low_pass(self):
        ag = self.make(["H1", "H2"], {"C1": 95, "C2": 95},
                       {"H1": (60, True), "H2": (99, False), "C1": (95, True), "C2": (92, True)})
        ag.upgrade_pass(closing=False)
        self.assertEqual(ag.arena.released()[0], "H2")

    def test_rejected_offer_tries_next_at_once(self):
        ag = self.make(["H1"], {"C1": 95, "C2": 90},
                       {"H1": (30, True), "C1": (90, True), "C2": (80, True)}, reject={"C1"})
        ag.upgrade_pass(closing=False)
        self.assertEqual(ag.arena.released(), ["H1"])
        self.assertIn("C2", ag.held)
        self.assertEqual(ag.open_slots("R1"), 0)

    def test_claimed_replacement_does_not_count_as_ready(self):
        ag = self.make(["H1"], {"C1": 95, "C2": 90},
                       {"H1": (30, True), "C1": (90, True), "C2": (80, True)}, claimed={"C2"})
        ag.upgrade_pass(closing=False)
        self.assertEqual(ag.arena.released(), [])                   # only one unclaimed replacement

    def tearDown(self):
        A.NO_RELEASE_AFTER = "16:15"


if __name__ == "__main__":
    unittest.main()
