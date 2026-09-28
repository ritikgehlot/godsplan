"""Unit tests for agent.py parsers and response-shape helpers. No network.

    python -m unittest discover -s tests -v
"""
import os, sys, unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("ARENA_KEY", "test")          # agent never reads it at import; belt and braces
import agent as A                                   # noqa: E402


class ScoreTests(unittest.TestCase):
    def test_formats(self):
        cases = {None: None, "": None, "N/A": None, "71/100": 71, "7.1/10": 71, "0.71": 71,
                 "71%": 71, 71: 71, 7.1: 71, 0.71: 71, "71": 71, "3.5/5": 70, "71 / 100": 71,
                 "7/0": None, True: None}
        for raw, want in cases.items():
            got = A.parse_score100(raw)
            if want is None:
                self.assertIsNone(got, raw)
            else:
                self.assertAlmostEqual(got, want, places=6, msg=raw)


class DaysTests(unittest.TestCase):
    def test_formats(self):
        cases = {None: None, "": None, "N/A": None, "immediate": 0, "Immediate joiner": 0,
                 "none": 0, "None": 0, "nil": 0, "2 months": 60, "3 weeks": 21, "45 days": 45,
                 "one month": 30, "a month": 30, 45: 45, "serving notice": None,
                 "serving notice, 15 days left": 15, "90d": 90}
        for raw, want in cases.items():
            got = A.parse_days(raw)
            if want is None:
                self.assertIsNone(got, raw)
            else:
                self.assertAlmostEqual(got, want, msg=raw)

    def test_word_numbers_need_word_boundaries(self):
        self.assertIsNone(A.parse_days("done"))        # 'one' inside 'done' must not become 1


class LakhTests(unittest.TestCase):
    def test_formats(self):
        cases = {None: None, "": None, "N/A": None, "negotiable": None, "12 LPA": 12,
                 "12,00,000": 12, "1.2 Cr": 120, "80k/month": 9.6, "25L": 25, "18 lakh": 18,
                 "₹ 18,00,000": 18, "1,50,000/month": 18, 1200000: 12, 12: 12,
                 "12 LPA (development role)": 12}
        for raw, want in cases.items():
            got = A.parse_lakh(raw)
            if want is None:
                self.assertIsNone(got, raw)
            else:
                self.assertAlmostEqual(got, want, places=6, msg=raw)


class YearsTests(unittest.TestCase):
    def test_formats(self):
        self.assertIsNone(A.parse_years(None))
        self.assertIsNone(A.parse_years("N/A"))
        self.assertEqual(A.parse_years("5 yrs"), 5)
        self.assertEqual(A.parse_years("18 months"), 1.5)
        self.assertEqual(A.parse_years(7), 7)


class SkillTests(unittest.TestCase):
    def setUp(self):
        A.VOCAB.clear(); A._canon_cache.clear()

    def tearDown(self):
        A.VOCAB.clear(); A._canon_cache.clear()

    def test_aliases(self):
        self.assertEqual(A.skill_set("k8s"), {"kubernetes"})
        self.assertEqual(A.skill_set("Node.js"), {"nodejs"})
        self.assertEqual(A.skill_set("Postgres, Python (5 yrs)"), {"postgresql", "python"})
        self.assertEqual(A.skill_set(["C#", ".NET"]), {"csharp", "dotnet"})

    def test_ci_cd_not_split(self):
        self.assertEqual(A.skill_set("Docker/CI/CD/AWS"), {"docker", "cicd", "aws"})
        self.assertEqual(A.skill_set("CI / CD; Terraform"), {"cicd", "terraform"})
        self.assertEqual(A.skill_set(["CI/CD"]), {"cicd"})

    def test_shapes(self):
        self.assertEqual(A.skill_set(None), set())
        self.assertEqual(A.skill_set(""), set())
        self.assertEqual(A.skill_set([{"name": "Python"}, {"skill": "Go"}]), {"python", "go"})
        self.assertEqual(A.skill_set({"Python": 3, "SQL": 1}), {"python", "sql"})

    def test_typo_maps_onto_vocab(self):
        A.VOCAB.update({"python", "kubernetes", "java"})
        self.assertEqual(A.coverage(A.skill_set("Pyhton, k8s"), {"python", "kubernetes"}), 1.0)
        self.assertEqual(A.canon("javascript"), "javascript")   # must not collapse onto 'java'


class NameKeyTests(unittest.TestCase):
    def test_same_person(self):
        a = A.name_key({"name": "Sharma, Rahul", "role": "Backend Engineer"})
        b = A.name_key({"name": "rahul  sharma", "role": "Backend Engineer"})
        self.assertEqual(a, b)

    def test_missing(self):
        self.assertIsNone(A.name_key({}))
        self.assertIsNone(A.name_key({"name": ""}))


class TruthyTests(unittest.TestCase):
    def test_claimed_values(self):
        for v in (None, False, 0, 0.0, "", "false", "False", "no", "0", "null", "none", " NO "):
            self.assertFalse(A.truthy(v), repr(v))
        for v in (True, 1, "true", "yes", "1", "team_7", ["x"]):
            self.assertTrue(A.truthy(v), repr(v))


class VerdictTests(unittest.TestCase):
    def test_mock_shape(self):
        self.assertEqual(A.assessment_verdict({"verified_assessment": 71, "reference_check": "passed"}),
                         (71, True))
        self.assertEqual(A.assessment_verdict({"verified_assessment": 71, "reference_check": "failed"}),
                         (71, False))

    def test_prefers_verified_over_self_reported(self):
        s, ok = A.assessment_verdict({"self_reported_assessment": 95, "claimed_score": 90,
                                      "verified_assessment": "62/100", "reference_score": 4})
        self.assertEqual(s, 62)
        self.assertTrue(ok)

    def test_nested_and_flags(self):
        self.assertEqual(A.assessment_verdict({"assessment": {"score": 0.8, "verified": True}}), (80, True))
        self.assertFalse(A.assessment_verdict({"score": 80, "verified": False})[1])
        self.assertFalse(A.assessment_verdict({"score": 80, "fabricated": True})[1])
        self.assertTrue(A.assessment_verdict({"score": 80, "fabricated": "false"})[1])
        self.assertFalse(A.assessment_verdict({"score": 80, "flags": ["employer_mismatch"]})[1])
        self.assertFalse(A.assessment_verdict({"reference_check": "Could not verify employment"})[1])
        self.assertTrue(A.assessment_verdict({"reference_check": "no mismatch found"})[1])
        self.assertTrue(A.assessment_verdict({"reference_check": "positive, no concerns"})[1])

    def test_junk(self):
        self.assertEqual(A.assessment_verdict(None), (None, True))
        self.assertEqual(A.assessment_verdict([]), (None, True))
        self.assertEqual(A.assessment_verdict({}), (None, True))


class RiskTests(unittest.TestCase):
    def test_unverified_is_not_green(self):
        base = {"assessment": "70/100", "notes": ""}
        clean = A.heuristic_risk(base, {})
        self.assertGreater(A.heuristic_risk(dict(base, notes="employer unverified"), {}), clean)
        self.assertGreater(A.heuristic_risk(dict(base, notes="reference not verified"), {}), clean)
        self.assertGreater(A.heuristic_risk(dict(base, notes="could not be verified"), {}), clean)
        self.assertLess(A.heuristic_risk(dict(base, notes="verified employer, strong reference"), {}), clean)

    def test_notes_as_list(self):
        self.assertGreater(A.heuristic_risk({"assessment": 70, "notes": ["accepted another offer"]}, {}), 0.3)


class ShapeTests(unittest.TestCase):
    def test_as_items(self):
        recs = [{"candidate_id": "C1"}, {"candidate_id": "C2"}]
        self.assertEqual(A.as_items(recs), recs)
        self.assertEqual(A.as_items({"results": recs, "page": 0}, "results"), recs)
        self.assertEqual(A.as_items({"results": []}, "results"), [])
        self.assertEqual(A.as_items({"C1": {"name": "a"}}), [{"name": "a", "id": "C1"}])
        self.assertEqual(A.as_items(None), [])
        self.assertEqual(A.as_items("oops"), [])
        self.assertEqual(A.as_items([1, "x", {"id": 1}]), [{"id": 1}])

    def test_rec_id(self):
        self.assertEqual(A.rec_id({"candidate_id": 7}), "7")
        self.assertEqual(A.rec_id({"id": "C9"}), "C9")
        self.assertIsNone(A.rec_id({}))
        self.assertIsNone(A.rec_id("C1"))

    def test_phase(self):
        for raw, want in (("recon", "recon"), ("MARKET", "market"), ("market_open", "market"),
                          ("Closing", "closing"), ("closed", "closed"), ("pre_start", "closed"),
                          ("ok", None), (None, None), (3, None)):
            self.assertEqual(A.norm_phase(raw), want, raw)

    def test_last_page(self):
        self.assertTrue(A.is_last_page({"results": [1], "has_more": False}, 100, 100, 100))
        self.assertFalse(A.is_last_page({"results": [1], "has_more": "true"}, 7, 100, 107))
        self.assertTrue(A.is_last_page({"total": 250}, 50, 100, 250))
        self.assertFalse(A.is_last_page({"total": 250}, 100, 100, 200))
        self.assertFalse(A.is_last_page([], 50, 50, 50))         # server caps pages at 50: keep going
        self.assertTrue(A.is_last_page([], 23, 50, 73))

    def test_offer_outcome(self):
        self.assertEqual(A.offer_outcome({"accepted": True}), (True, False, None))
        self.assertEqual(A.offer_outcome({"accepted": True, "already_yours": True}), (True, True, None))
        self.assertEqual(A.offer_outcome({"accepted": False, "reason": "already_signed"}),
                         (False, False, "already_signed"))
        self.assertEqual(A.offer_outcome({"accepted": "false", "reason": "same_person_already_signed"})[2],
                         "same_person_already_signed")
        self.assertEqual(A.offer_outcome({"status": "rejected", "error": "Requisition full"})[2],
                         "requisition_full")
        self.assertTrue(A.offer_outcome({"status": "signed"})[0])
        self.assertFalse(A.offer_outcome(None)[0])
        self.assertFalse(A.offer_outcome([])[0])

    def test_offer_reason_from_http_error(self):
        self.assertEqual(A.offer_reason('400: {"error":"role_mismatch"}'), "role_mismatch")
        self.assertIsNone(A.offer_reason("gave up on /offer"))


class FakeArena:
    """Records offers; answers like the mock. No network."""
    base, credits_remaining = "http://127.0.0.1:0", 50000

    def __init__(self, full_after=None):
        self.offers, self.full_after = [], full_after

    def offer(self, cid, rid):
        self.offers.append((cid, rid))
        return {"accepted": True}


class SlotAccountingTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()
        A.STATE_PATH = os.path.join(self.tmp, "state.json")
        A.DECISIONS_PATH = os.path.join(self.tmp, "decisions.jsonl")
        self.agent = A.Agent(FakeArena())
        self.agent.reqs = {"R1": {"req_id": "R1", "headcount": 3}}
        self.agent.bars = {"R1": A.Agent.parse_bar(self.agent.reqs["R1"])}
        for c in ("C1", "C2", "C3"):
            self.agent.profiles[c] = {"cid": c, "claimed": False, "assess": 80, "notice": 0, "ctc": 1,
                                      "skills": [], "risk": 0.05, "notes": "", "exp": 3, "email": ""}

    def test_fresh_disk_restart_counts_server_filled(self):
        a = self.agent
        a.filled_srv["R1"] = 1                        # server: one slot filled before the crash
        self.assertEqual(a.open_slots("R1"), 2)
        self.assertTrue(a.try_offer("C1", "R1"))
        self.assertTrue(a.try_offer("C2", "R1"))
        self.assertEqual(a.open_slots("R1"), 0)       # 1 old + 2 new: full, no requisition_full
        self.assertFalse(a.try_offer("C3", "R1"))
        self.assertEqual(len(a.arena.offers), 2)

    def test_release_frees_a_slot(self):
        a = self.agent
        a.filled_srv["R1"] = 3
        a.held.update({"C1": "R1", "C2": "R1"})
        self.assertEqual(a.open_slots("R1"), 0)
        a.arena.release = lambda cid: {"released": True}
        self.assertTrue(a.release("C1", why="test"))
        self.assertEqual(a.open_slots("R1"), 1)

    def test_local_holds_win_without_server_count(self):
        a = self.agent
        a.held.update({"C1": "R1"})
        self.assertEqual(a.open_slots("R1"), 2)


if __name__ == "__main__":
    unittest.main()
