"""Contract tests use an injected fake model, never a network/model service."""
from copy import deepcopy
import json
from pathlib import Path
import unittest

from broker.core import analyze_case
from broker.pipeline import PipelineError, STAGES, run_pipeline, stage_schema


FIXTURE = Path(__file__).resolve().parents[1] / "data/ap001.json"


def context_from(prompt):
    return json.loads(prompt.split("\n\nCASE_CONTEXT_JSON\n", 1)[1])


def valid_packet(stage, context):
    """A small explicit response fixture; production never imports this helper."""
    controller, case = context["controller"], context["case"]
    refs = []
    for source in case["sources"]:
        for locator, text in source["sections"].items():
            refs.append({"source_id": source["source_id"], "source_type": source["source_type"],
                "section_or_locator": locator, "document_date": source["recorded_at"],
                "effective_from": source["effective_from"] or "UNKNOWN",
                "effective_to": source["effective_to"] or "UNKNOWN", "excerpt": text})
    unknowns = [{"field": item["field"], "description": "Not supplied in the original records.",
                 "source_ids": [item["source_ref"]["source_id"]]} for item in context["preflight"]["unknowns"]]
    payloads = {
        "watcher": {"events": [{"event_id": "E1", "entity": "AP-001", "field": "reported_staffing",
            "fact": "The client reports four new hires; vehicle assignments remain unknown.",
            "prior_value": None, "reported_value": "Four reported new hires", "evidence_state": "REPORTED",
            "date": "2026-09-26", "source_ids": ["AP-MSG-001"], "unresolved_questions": ["Which vehicles are assigned?"]}],
            "open_questions": [{"question_id": "Q1", "question": "Which vehicles are assigned?",
                "reason": "The message supplies no vehicle assignments.", "source_ids": ["AP-MSG-001"]}], "next_stage": "matcher"},
        "matcher": {"comparisons": [{"comparison_id": "C1", "event_id": "E1",
            "historical_state": "R1 is retained in the supplied record.", "current_state": "Use the applicable issued schedule.",
            "relevant_policy_language": "The review should establish driver identity, assignment start date and assigned vehicle.",
            "potential_impact": "Vehicle assignment verification remains open.", "requirement_basis": "REQUIRED_BY_SOURCE",
            "conflict_state": "NONE", "remaining_decision": "Broker should verify vehicle assignments.",
            "source_ids": ["AP-POL-001", "AP-MSG-001"]}], "required_verification": [{
                "verification_id": "V1", "request": "Confirm assigned vehicles", "reason": "Required by the fictional policy review passage.",
                "classification": "REQUIRED_BY_SOURCE", "source_ids": ["AP-POL-001"]}],
            "optional_suggestions": [], "conflicts": [], "next_stage": "closer"},
        "closer": {"drafts": [{"draft_id": "D1", "recipient_placeholder": "Dana Cole — address to be confirmed by broker",
            "subject": "Driver assignment clarification", "draft_message": "Please confirm each new driver's assigned vehicle.",
            "question_to_source_map": [{"question": "Which vehicle is each driver assigned to?", "reason": "Assignment facts are missing.",
                "source_ids": ["AP-MSG-001", "AP-POL-001"]}], "open_issues": ["Vehicle assignments"], "approval_state": "UNSENT"}],
            "no_draft_reason": None, "broker_review_checklist": ["Review recipient and exact draft text before sending."],
            "approval_state": "UNSENT", "next_stage": "scribe"},
        "scribe": {"proposed_changes": [{"change_id": "P1", "entity": "AP-001", "field": "reported_staffing",
            "prior_value": None, "proposed_value": "Four hires reported; assignments unresolved.", "effective_date": "UNKNOWN",
            "evidence_state": "REPORTED", "source_ids": ["AP-MSG-001"], "conflict_state": "NONE",
            "reason": "Preserve the report without claiming a policy change.", "write_status": "UNCOMMITTED"}],
            "learning_proposals": [], "conflicts": [], "approval_needed": [
                {"proposal_id": "P1", "decision": "Approve or reject this reported-fact proposal."},
                {"proposal_id": "D1", "decision": "Approve exact recipient and draft text separately."}],
            "draft_follow_up_refs": ["D1"], "write_status": "UNCOMMITTED"},
    }
    return {"schema_version": "broker-handoff/v1", "run_id": controller["run_id"], "case_id": controller["case_id"],
        "source_revision": controller["source_revision"], "stage": stage, "status": "NEEDS_REVIEW", "reason_code": None,
        "missing_inputs": [], "source_refs": refs, "unknowns": unknowns, "cache_event": "UNKNOWN",
        "cache_receipt_id": None, "payload": payloads[stage]}


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.case = json.loads(FIXTURE.read_text())
        self.preflight = analyze_case(self.case)
        self.calls = []

    def model(self, stage, prompt, schema):
        context = context_from(prompt)
        self.calls.append((stage, context, schema))
        return valid_packet(stage, context)

    def test_four_distinct_calls_forward_full_prior_packets_and_controller_digest(self):
        original_case, original_preflight = deepcopy(self.case), deepcopy(self.preflight)
        observed = []
        result = run_pipeline(self.case, self.preflight, self.model, lambda stage, packet: observed.append((stage, packet)))
        self.assertEqual([call[0] for call in self.calls], list(STAGES))
        self.assertEqual(result["completed_stages"], list(STAGES))
        self.assertEqual(result["execution"]["model_calls"], 4)
        self.assertEqual(result["status"], "NEEDS_BROKER_REVIEW")
        self.assertEqual(result["actions"], {"messages_sent": 0, "records_updated": 0})
        self.assertEqual(result["source_revision"], self.preflight["source_revision"])
        for index, (stage, context, schema) in enumerate(self.calls):
            self.assertEqual(context["case"], original_case)
            self.assertEqual(context["preflight"], original_preflight)
            self.assertEqual(context["previous_packets"], result["stages"][:index])
            self.assertEqual(context["controller"]["completed_stages"], list(STAGES[:index]))
            self.assertEqual(context["controller"]["run_id"], result["run_id"])
            self.assertEqual(schema, stage_schema(stage))
            self.assertEqual(observed[index], (stage, result["stages"][index]))
        self.assertEqual(self.case, original_case)
        self.assertEqual(self.preflight, original_preflight)

    def test_all_nested_schema_objects_are_strict_with_all_properties_required(self):
        def check(node):
            if node.get("type") == "object":
                self.assertIs(node["additionalProperties"], False)
                self.assertEqual(set(node["required"]), set(node["properties"]))
                for child in node["properties"].values():
                    check(child)
            elif node.get("type") == "array":
                check(node["items"])
        for stage in STAGES:
            check(stage_schema(stage))

    def test_blocked_stage_is_returned_and_halts_all_downstream_calls(self):
        def invoke(stage, prompt, schema):
            packet = self.model(stage, prompt, schema)
            if stage == "matcher":
                packet.update(status="BLOCKED", reason_code="SOURCE_CONFLICT")
            return packet
        result = run_pipeline(self.case, self.preflight, invoke)
        self.assertEqual([c[0] for c in self.calls], ["watcher", "matcher"])
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["blocked_stage"], "matcher")
        self.assertEqual(result["completed_stages"], ["watcher"])
        self.assertEqual(result["execution"]["model_calls"], 2)

    def test_handoff_identity_and_role_mismatches_stop_immediately(self):
        for key, wrong in (("run_id", "other"), ("case_id", "other"), ("source_revision", "other"), ("stage", "scribe")):
            with self.subTest(key=key):
                self.calls = []
                def invoke(stage, prompt, schema):
                    packet = self.model(stage, prompt, schema)
                    packet[key] = wrong
                    return packet
                with self.assertRaises(PipelineError):
                    run_pipeline(self.case, self.preflight, invoke)
                self.assertEqual(len(self.calls), 1)

    def test_forged_quotes_dates_types_and_material_source_ids_are_rejected(self):
        mutations = [
            lambda p: p["source_refs"][0].update(excerpt="All drivers are covered."),
            lambda p: p["source_refs"][0].update(document_date="2026-01-01"),
            lambda p: p["source_refs"][0].update(effective_from="2026-01-01"),
            lambda p: p["source_refs"][0].update(source_type="issued_endorsement_fixture"),
            lambda p: p["source_refs"][0].update(section_or_locator="OTHER#identity"),
            lambda p: p["payload"]["events"][0].update(source_ids=["INVENTED"]),
            lambda p: p.update(source_refs=[r for r in p["source_refs"] if r["source_id"] != "AP-MSG-001"]),
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                self.calls = []
                def invoke(stage, prompt, schema):
                    packet = self.model(stage, prompt, schema)
                    mutate(packet)
                    return packet
                with self.assertRaises(PipelineError) as caught:
                    run_pipeline(self.case, self.preflight, invoke)
                self.assertEqual(caught.exception.code, "INVALID_CITATION")
                self.assertEqual(len(self.calls), 1)

    def test_unsent_uncommitted_and_unknown_cache_are_enforced(self):
        for target, mutate in [
            ("closer", lambda p: p["payload"].update(approval_state="SENT")),
            ("closer", lambda p: p["payload"]["drafts"][0].update(approval_state="SENT")),
            ("scribe", lambda p: p["payload"].update(write_status="APPLIED")),
            ("scribe", lambda p: p["payload"]["proposed_changes"][0].update(write_status="APPLIED")),
            ("watcher", lambda p: p.update(cache_event="HIT", cache_receipt_id="fabricated")),
            ("watcher", lambda p: p.update(status="BLOCKED", reason_code=None)),
        ]:
            with self.subTest(stage=target, mutation=mutate):
                self.calls = []
                def invoke(stage, prompt, schema):
                    packet = self.model(stage, prompt, schema)
                    if stage == target:
                        mutate(packet)
                    return packet
                with self.assertRaises(PipelineError):
                    run_pipeline(self.case, self.preflight, invoke)
                self.assertEqual(len(self.calls), STAGES.index(target) + 1)

    def test_unknowns_and_downstream_references_cannot_be_fabricated_or_dropped(self):
        for target, mutate, code in [
            ("watcher", lambda p: p.update(unknowns=[]), "UNKNOWN_DROPPED"),
            ("matcher", lambda p: p["payload"]["comparisons"][0].update(event_id="not-from-watcher"), "HANDOFF_MISMATCH"),
            ("scribe", lambda p: p["payload"].update(draft_follow_up_refs=["not-from-closer"]), "HANDOFF_MISMATCH"),
            ("scribe", lambda p: p["payload"].update(approval_needed=[]), "INVALID_PACKET"),
        ]:
            with self.subTest(stage=target, mutation=mutate):
                def invoke(stage, prompt, schema):
                    packet = self.model(stage, prompt, schema)
                    if stage == target:
                        mutate(packet)
                    return packet
                with self.assertRaises(PipelineError) as caught:
                    run_pipeline(self.case, self.preflight, invoke)
                self.assertEqual(caught.exception.code, code)

    def test_observer_cannot_mutate_accepted_handoff(self):
        def observer(stage, packet):
            packet["case_id"] = "changed-by-observer"
            packet["payload"].clear()
        result = run_pipeline(self.case, self.preflight, self.model, observer)
        self.assertTrue(all(p["case_id"] == self.case["case_id"] for p in result["stages"]))
        self.assertTrue(all(p["payload"] for p in result["stages"]))

    def test_needs_review_conflict_continues_to_clarification_but_cannot_be_laundered(self):
        conflict = {"conflict_id": "X1", "description": "An unresolved source question.",
                    "resolution_required": "Broker verification", "source_ids": ["AP-MSG-001"]}
        for preserve in (True, False):
            with self.subTest(preserve=preserve):
                self.calls = []
                def invoke(stage, prompt, schema):
                    packet = self.model(stage, prompt, schema)
                    if stage == "matcher":
                        packet["payload"]["conflicts"] = [deepcopy(conflict)]
                        packet["payload"]["comparisons"][0]["conflict_state"] = "UNRESOLVED"
                    if stage == "scribe":
                        packet["payload"]["conflicts"] = [deepcopy(conflict)]
                        if preserve:
                            packet["payload"]["proposed_changes"][0]["conflict_state"] = "UNRESOLVED"
                    return packet
                if preserve:
                    result = run_pipeline(self.case, self.preflight, invoke)
                    self.assertEqual(result["completed_stages"], list(STAGES))
                    self.assertEqual(result["stages"][2]["payload"]["approval_state"], "UNSENT")
                else:
                    with self.assertRaises(PipelineError) as caught:
                        run_pipeline(self.case, self.preflight, invoke)
                    self.assertEqual(caught.exception.code, "UNKNOWN_DROPPED")
                self.assertEqual(len(self.calls), 4)

    def test_missing_extra_fields_wrong_routing_and_omitted_comparisons_fail_closed(self):
        for target, mutate in [
            ("watcher", lambda p: p.pop("unknowns")),
            ("watcher", lambda p: p["payload"]["events"][0].update(extra="unsupported")),
            ("watcher", lambda p: p["payload"].update(next_stage="scribe")),
            ("matcher", lambda p: p["payload"].update(comparisons=[])),
        ]:
            with self.subTest(stage=target, mutation=mutate):
                self.calls = []
                def invoke(stage, prompt, schema):
                    packet = self.model(stage, prompt, schema)
                    if stage == target:
                        mutate(packet)
                    return packet
                with self.assertRaises(PipelineError):
                    run_pipeline(self.case, self.preflight, invoke)
                self.assertEqual(len(self.calls), STAGES.index(target) + 1)

    def test_changed_source_revision_never_invokes_model(self):
        self.case["sources"][0]["sections"]["AP-PROFILE-001#identity"] += " Changed."
        with self.assertRaises(PipelineError) as caught:
            run_pipeline(self.case, self.preflight, self.model)
        self.assertEqual(caught.exception.code, "HANDOFF_MISMATCH")
        self.assertEqual(self.calls, [])

    def test_model_exception_becomes_pipeline_error_without_echoing_sensitive_log(self):
        def failing_model(stage, prompt, schema):
            raise RuntimeError("sensitive provider log")
        with self.assertRaises(PipelineError) as caught:
            run_pipeline(self.case, self.preflight, failing_model)
        self.assertEqual(caught.exception.code, "TOOL_ERROR")
        self.assertEqual(caught.exception.stage, "watcher")
        self.assertNotIn("sensitive", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
