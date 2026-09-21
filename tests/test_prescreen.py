"""Tests for prescreen.py -- the pre-launch packet screening gate.

Covers the real blocker patterns seen 2026-09-14:
  1. Scale AI "in person ... 3 times a week" office question
  2. Snorkel AI "relocating ... hybrid in-office" question
  3. Fleetio's 4 unmapped free-text experience screeners
  4. OpenAI Ashby pattern (office + arbitration + personally-completed) via
     employer patterns on a packet with no form intel
  5. Clean packet (AIO Logic style) -> CLEAN
  6. park_lead overwrites stale status_reason and uses conventional fields
  7. "own original, unassisted writing" essay gate

Run: python3 test_prescreen.py
"""

import json
import os
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.abspath(__file__))
ENGINES = os.path.join(BASE, "..", "engines")
sys.path.insert(0, ENGINES)
# Isolate the field-question backlog: prescreen routes through
# field_question_protocol, whose backlog path defaults to the operator's
# real data dir. Tests must never read or write it.
os.environ["FIELD_PROTOCOL_DIR"] = tempfile.mkdtemp(prefix="keel-test-frp-")
import prescreen

BANK = json.load(open(os.path.join(ENGINES, "answer_bank.example.json")))

GENUINE_WORDS = ("essay", "wording", "travel", "attest", "reference", "applicant",
                 "salary", "degree", "location", "hybrid", "onsite",
                 "relocation", "captcha", "account", "login")


def make_packet(company, intel_lines, role_id="TEST-ROLE-1"):
    intel = "\n".join(intel_lines)
    brief = (
        "Submit a job application for the applicant.\n"
        "GATES (stop conditions -- obey exactly):\n"
        "  - travel_attestation: STOP and ask the applicant.\n"
        "FORM INTEL \u2014 VERIFIED PRE-LAUNCH (do not re-derive; use these exact labels):\n"
        f"{intel}\n"
        "STEP 3 \u2014 COMMIT TECHNIQUE FOR GREENHOUSE: live-option click + per-field verify\n"
    )
    return {"role_id": role_id, "company": company, "title": "Test Role",
            "brief": brief}


class TestScreenPacket(unittest.TestCase):
    def test_scale_ai_office_3x_week_parks(self):
        p = make_packet("Scale AI", [
            "  - [dropdown] Are you open to working in person in our San Francisco "
            "or New York office 3 times a week?",
            "  - [text] First Name*",
            "  - [text] Email*",
        ])
        res = prescreen.screen_packet(p, BANK, {})
        self.assertEqual(res["verdict"], "PARK")
        self.assertTrue(any("travel" in r or "applicant" in r for r in res["reasons"]))

    def test_snorkel_relocation_hybrid_parks(self):
        p = make_packet("Snorkel AI", [
            "  - [dropdown] Are you open to relocating to either the SF Bay Area or "
            "New York and working in one of our office locations in a hybrid "
            "in-office arrangement?*",
            "  - [text] First Name*",
        ])
        res = prescreen.screen_packet(p, BANK, {})
        self.assertEqual(res["verdict"], "PARK")
        self.assertTrue(any("relocation" in r or "hybrid" in r for r in res["reasons"]))

    def test_fleetio_unmapped_screeners_park(self):
        p = make_packet("Fleetio", [
            "  - [text] Have you built a partner or channel enablement program from "
            "scratch (no existing playbook, curriculum, or cadence to inherit)?*",
            "  - [text] Has your enablement experience spanned multiple stages -- "
            "sales, onboarding, AND ongoing success/adoption -- rather than just "
            "one of those?*",
            "  - [text] Have you worked directly with external reseller/channel "
            "partner organizations -- their business and product/technical staff "
            "-- not just internal sales teams?*",
            "  - [text] Can you point to a specific instance where you drove a "
            "cross-functional team to act on something for partner readiness, "
            "without having formal authority over them?*",
            "  - [text] First Name*",
            "  - [text] Email*",
        ])
        res = prescreen.screen_packet(p, BANK, {})
        self.assertEqual(res["verdict"], "PARK")
        unmapped = [r for r in res["reasons"]
                    if "applicant's own input" in r or "not in answer bank" in r]
        self.assertEqual(len(unmapped), 4,
                         f"expected 4 unmapped reasons, got: {res['reasons']}")

    def test_openai_ashby_pattern_parks_without_intel(self):
        # Ashby packets carry no form intel; the employer pattern (confirmed on
        # two live 2026-09-14 encounters) must still PARK.
        p = {"role_id": "OPENAI-TEST-1", "company": "OpenAI",
             "title": "Test Role",
             "brief": "Submit a job application for the applicant to OpenAI.\n"
                      "FORM INTEL \u2014 VERIFIED PRE-LAUNCH:\n"
                      "  - resume file: resumes/Test_Applicant.pdf\n"
                      "STEP 3 \u2014 COMMIT TECHNIQUE: generic\n"}
        patterns = {"openai": ["three days per week",
                               "arbitration agreement",
                               "personally completed"]}
        res = prescreen.screen_packet(p, BANK, patterns)
        self.assertEqual(res["verdict"], "PARK")
        self.assertEqual(len(res["reasons"]), 3)

    def test_clean_packet_passes(self):
        p = make_packet("AIO Logic", [
            "  - [text] First Name*",
            "  - [text] Last Name*",
            "  - [text] Email*",
            "  - [dropdown] Country",
            "  - [text] Phone",
            "  - [dropdown] School",
            "  - [dropdown] Degree",
            "  - [dropdown] Discipline",
            "  - [text] Start date year",
            "  - [text] End date year",
            "  - [text] LinkedIn Profile",
            "  - [text] Website",
            "  - resume file: resumes/Test_Applicant.pdf",
        ], role_id="AIOLOGIC-TEST-1")
        res = prescreen.screen_packet(p, BANK, {})
        self.assertEqual(res["verdict"], "CLEAN", f"reasons: {res['reasons']}")
        self.assertEqual(res["reasons"], [])

    def test_essay_gate_parks(self):
        p = make_packet("Perplexity", [
            "  - [text] Answer in your own original, unassisted writing: why do "
            "you want this role?*",
            "  - [text] First Name*",
        ])
        res = prescreen.screen_packet(p, BANK, {})
        self.assertEqual(res["verdict"], "PARK")
        self.assertTrue(any("essay" in r for r in res["reasons"]))

    def test_template_gates_do_not_false_positive(self):
        # The brief template's own GATES section mentions travel/attest as
        # stop-conditions; screening is scoped to FORM INTEL so a clean packet
        # must not PARK on template language.
        p = make_packet("CleanCo", [
            "  - [text] First Name*",
            "  - [text] Last Name*",
            "  - [text] Email*",
        ])
        res = prescreen.screen_packet(p, BANK, {})
        self.assertEqual(res["verdict"], "CLEAN")

    def test_empty_company_matches_no_employer_pattern(self):
        # Regression: "" in <any string> is True, so a packet with company
        # None/"" must not match every employer pattern.
        p = make_packet(None, [
            "  - [text] First Name*",
            "  - [text] Last Name*",
            "  - [text] Email*",
        ])
        p["company"] = None
        patterns = {"openai": ["three days per week"],
                    "perplexity": ["unassisted writing"]}
        res = prescreen.screen_packet(p, BANK, patterns)
        self.assertEqual(res["verdict"], "CLEAN", f"reasons: {res['reasons']}")

    def test_company_fallback_from_brief_target_line(self):
        # company field empty but brief names the employer: pattern applies.
        p = make_packet(None, [
            "  - resume file: resumes/Test_Applicant.pdf",
        ])
        p["company"] = None
        p["brief"] = p["brief"].replace(
            "Submit a job application for the applicant.",
            "Submit a job application for the applicant to OpenAI for the role \"X\".")
        patterns = {"openai": ["three days per week"]}
        res = prescreen.screen_packet(p, BANK, patterns)
        self.assertEqual(res["verdict"], "PARK")


class TestParkLead(unittest.TestCase):
    def test_stale_status_reason_overwritten(self):
        with tempfile.TemporaryDirectory() as qdir:
            stale = ("verify-retry: posting confirmed live via ATS API; "
                     "promoted READY.")
            lead = {"role_id": "STRIPE-TEST-1", "company": "Stripe",
                    "title": "Test", "status": "READY",
                    "status_reason": stale, "unresolved": []}
            json.dump([lead], open(os.path.join(qdir, "standard-queue.json"), "w"))
            json.dump([], open(os.path.join(qdir, "needs_input-queue.json"), "w"))

            reasons = ["Required office-commitment question needs the applicant's "
                       "explicit answer (travel): \"3 times a week\""]
            out = prescreen.park_lead("STRIPE-TEST-1", reasons, queue_dir=qdir)
            self.assertTrue(out["ok"], out)

            std = json.load(open(os.path.join(qdir, "standard-queue.json")))
            ni = json.load(open(os.path.join(qdir, "needs_input-queue.json")))
            self.assertEqual(std, [])
            self.assertEqual(len(ni), 1)
            parked = ni[0]
            # Conventional fields only.
            self.assertEqual(parked["status"], "PARKED-NEEDS-INPUT")
            self.assertNotIn("promoted READY", parked["status_reason"])
            self.assertEqual(parked["unresolved"], reasons)
            self.assertIn("applicant", parked["gate_note"])
            self.assertIn("prescreen", parked["queue_notes"])
            # Genuine-input words present so verify_retry never resurrects it.
            blob = " ".join(parked["unresolved"]).lower()
            self.assertTrue(any(w in blob for w in GENUINE_WORDS))
            # Backup taken.
            backups = [d for d in os.listdir(qdir) if d.startswith("_backup-")]
            self.assertEqual(len(backups), 1)

    def test_missing_lead_no_mutation(self):
        with tempfile.TemporaryDirectory() as qdir:
            json.dump([], open(os.path.join(qdir, "standard-queue.json"), "w"))
            json.dump([], open(os.path.join(qdir, "needs_input-queue.json"), "w"))
            out = prescreen.park_lead("NOPE-1", ["reason (travel)"],
                                      queue_dir=qdir, backup=False)
            self.assertFalse(out["ok"])
            self.assertIn("not found", out["error"])


class TestSyntheticTelemetryIsolation(unittest.TestCase):
    """J-20260918-2130-gate-2313: fixture emissions must never reach the
    real production events.jsonl.

    The polluter was TestParkLead.test_stale_status_reason_overwritten,
    which calls prescreen.park_lead("STRIPE-TEST-1", ...) against a tmpdir
    queue — but park_lead emitted gate telemetry to the REAL events file
    (~43 rows since 2026-09-15). The is_synthetic_role_id guard in
    park_lead is the fix; the canary below fails if any test ever writes
    to the real path again.
    """

    def _real_events_stat(self):
        import log_event
        p = log_event.EVENTS
        try:
            st = os.stat(p)
            return p, st.st_size, st.st_mtime_ns
        except FileNotFoundError:
            return p, None, None

    def test_is_synthetic_role_id_semantics(self):
        # Fixture ids are synthetic ...
        self.assertTrue(prescreen.is_synthetic_role_id("STRIPE-TEST-1"))
        self.assertTrue(prescreen.is_synthetic_role_id("TEST-LEAD-1"))
        self.assertTrue(prescreen.is_synthetic_role_id("R-STALE-FIXTURE-2"))
        # ... but real sweep traffic keeps flowing: FIRETEST-* are real
        # leads, and bare R-STALE carries no synthetic token (its
        # parked-sweep pollution is a different emitter, owned by the
        # octopus isolation fix, not this guard).
        self.assertFalse(prescreen.is_synthetic_role_id("FIRETEST-9"))
        self.assertFalse(prescreen.is_synthetic_role_id("R-STALE"))
        self.assertFalse(prescreen.is_synthetic_role_id(
            "ARM1C-COINBASE-SR-STRAT-PROGRAM-LEAD-20260915"))
        self.assertFalse(prescreen.is_synthetic_role_id(""))

    def test_canary_fixture_park_emits_no_production_telemetry(self):
        """CANARY: fails if a fixture park_lead writes to the real
        production events.jsonl. Snapshots the real file (size+mtime)
        around the exact polluting call shape and demands zero delta."""
        _, size0, mtime0 = self._real_events_stat()
        with tempfile.TemporaryDirectory() as qdir:
            lead = {"role_id": "STRIPE-TEST-1", "company": "Stripe",
                    "title": "Test", "status": "READY",
                    "status_reason": "stale", "unresolved": []}
            json.dump([lead],
                      open(os.path.join(qdir, "standard-queue.json"), "w"))
            json.dump([],
                      open(os.path.join(qdir, "needs_input-queue.json"), "w"))
            reasons = ["Required office-commitment question needs the "
                       "applicant's explicit answer (travel)"]
            out = prescreen.park_lead("STRIPE-TEST-1", reasons,
                                      queue_dir=qdir)
            self.assertTrue(out["ok"], out)
        _, size1, mtime1 = self._real_events_stat()
        self.assertEqual(
            (size1, mtime1), (size0, mtime0),
            "CANARY: fixture park_lead wrote to the real production "
            "events.jsonl — the synthetic-role guard was bypassed or removed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
