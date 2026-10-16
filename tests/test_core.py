"""Three behavioral validations of the fictional broker preflight."""
from copy import deepcopy
import json
from pathlib import Path
import unittest

from broker.core import CaseValidationError, analyze_case


FIXTURE = Path(__file__).resolve().parents[1] / "data" / "ap001.json"


class BrokerPreflightTests(unittest.TestCase):
    def setUp(self):
        self.case = json.loads(FIXTURE.read_text())

    def test_effective_dates_preserve_history_and_separate_receipt_time(self):
        yesterday = analyze_case(self.case, "2026-09-26T12:00:00-04:00")
        self.assertEqual(yesterday["current_schedule"]["revision"], "R1")
        self.assertEqual(len(yesterday["current_schedule"]["drivers"]), 3)
        self.assertEqual(yesterday["current_schedule"]["source_ref"]["locator"], "AP-POL-001#schedule")
        # The supplied endorsement is effective at midnight, received at 10:00.
        # A retrospective 08:00 view includes R2, without claiming it was known.
        morning = analyze_case(self.case, "2026-09-27T08:00:00-04:00")
        self.assertEqual(morning["current_schedule"]["revision"], "R2")
        self.assertEqual(len(morning["current_schedule"]["drivers"]), 5)
        self.assertEqual(morning["current_schedule"]["recorded_at"], "2026-09-27T10:00:00-04:00")
        self.assertEqual(morning["historical_schedules"][0]["revision"], "R1")
        self.assertIn("not a knowledge cutoff", morning["time_basis"])
        self.assertEqual([a["timing"] for a in morning["reported_assignments"]], ["START_DATE_REACHED", "START_DATE_REACHED", "PLANNED", "PLANNED"])
        # 01:00 UTC September 27 is still September 26 in the policy timezone.
        utc = analyze_case(self.case, "2026-09-27T01:00:00+00:00")
        self.assertEqual(utc["current_schedule"]["revision"], "R1")
        self.assertTrue(all(a["timing"] == "PLANNED" for a in utc["reported_assignments"]))
        expired = analyze_case(self.case, "2027-07-01T12:00:00-04:00")
        self.assertIsNone(expired["current_schedule"])
        self.assertEqual(expired["review_questions"][0]["id"], "applicable-schedule")

    def test_untrusted_normalized_records_cannot_cross_scope_or_invent_evidence(self):
        corruptions = [
            ("SCOPE_MISMATCH", lambda c: c["sources"][0].update(client_id="MC-002")),
            ("SCOPE_MISMATCH", lambda c: c["sources"][-1].update(policy_id="OTHER-POLICY")),
            ("INVALID_CITATION", lambda c: c["assignment_records"][0]["source_ref"].update(quote="All four drivers are covered.")),
            ("UNSUPPORTED_CLAIM", lambda c: c["assignment_records"][0].update(reported_start_date="2026-10-05")),
            ("UNSUPPORTED_CLAIM", lambda c: c["schedule_records"][1]["drivers"].append({"driver_id": "AP-D06", "name": "Jordan Park"})),
            ("SOURCE_AUTHORITY", lambda c: c["sources"][-1].update(source_type="client_message")),
            ("INVALID_DATE", lambda c: c.update(review_at="2026-09-27T12:00:00")),
            ("SCHEDULE_CONFLICT", lambda c: c["schedule_records"].append(deepcopy(c["schedule_records"][1]))),
        ]
        for expected, change in corruptions:
            with self.subTest(expected=expected, mutation=change):
                case = deepcopy(self.case)
                change(case)
                with self.assertRaises(CaseValidationError) as raised:
                    analyze_case(case)
                self.assertEqual(raised.exception.code, expected)

    def test_review_questions_preserve_unknowns_and_require_human_decisions(self):
        before = deepcopy(self.case)
        result = analyze_case(self.case)
        self.assertEqual(self.case, before, "Analysis must not mutate input evidence")
        self.assertEqual(result["status"], "NEEDS_REVIEW")
        self.assertEqual(result["execution"], {"kind": "deterministic_preflight", "model_called": False})
        self.assertEqual({q["id"] for q in result["review_questions"]}, {"schedule-AP-D06", "schedule-AP-D07", "vehicle-assignments"})
        self.assertEqual({u["field"] for u in result["unknowns"]}, {"vehicle_assignments", "premium", "limits", "coverage_determination"})
        self.assertTrue(result["human_decisions"])
        self.assertEqual(result["action_state"], {"messages_sent": False, "records_changed": False, "coverage_determined": False})
        by_id = {s["source_id"]: s for s in self.case["sources"]}
        for ref in result["evidence_refs"]:
            self.assertIn(ref["quote"], by_id[ref["source_id"]]["sections"][ref["locator"]])
        # An irrelevant newsletter color change must not generate a question.
        self.assertNotIn("AP-MSG-002", {ref["source_id"] for ref in result["evidence_refs"]})
        self.assertTrue(all(a["evidence_state"] == "CLIENT_REPORTED" for a in result["reported_assignments"]))


if __name__ == "__main__":
    unittest.main()
