"""Four separately invoked agents with controller-owned, validated handoffs.

This controller only produces review packets. Its callback is an observer, not
an action adapter. Citation validation establishes provenance, not the truth of
every interpretation made by a model; the broker still reviews the result.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4


STAGES = ("watcher", "matcher", "closer", "scribe")
PROMPTS = Path(__file__).resolve().parents[1] / "prompts"


class PipelineError(ValueError):
    def __init__(self, code: str, message: str, stage: str | None = None):
        self.code = code
        self.stage = stage
        super().__init__(message)


def _fail(code: str, message: str, stage: str | None = None) -> None:
    raise PipelineError(code, message, stage)


def _object(**properties: dict) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def _array(items: dict, *, nonempty: bool = False) -> dict:
    result = {"type": "array", "items": items}
    if nonempty:
        result["minItems"] = 1
    return result


def _enum(*values: str) -> dict:
    return {"type": "string", "enum": list(values)}


TEXT = {"type": "string", "minLength": 1}
OPTIONAL_TEXT = {"type": ["string", "null"], "minLength": 1}
STRINGS = _array(TEXT)
SOURCE_IDS = _array(TEXT, nonempty=True)
EVIDENCE_STATE = _enum("REPORTED", "DOCUMENT_CONFIRMED", "UNKNOWN")
CONFLICT_STATE = _enum("NONE", "UNRESOLVED")
QUESTION = _object(question_id=TEXT, question=TEXT, reason=TEXT, source_ids=SOURCE_IDS)
CONFLICT = _object(conflict_id=TEXT, description=TEXT, resolution_required=TEXT, source_ids=SOURCE_IDS)
SOURCE_REF = _object(source_id=TEXT, source_type=TEXT, section_or_locator=TEXT,
                     document_date=TEXT, effective_from=TEXT, effective_to=TEXT, excerpt=TEXT)
UNKNOWN = _object(field=TEXT, description=TEXT, source_ids=SOURCE_IDS)

PAYLOAD_SCHEMAS = {
    "watcher": _object(
        events=_array(_object(event_id=TEXT, entity=TEXT, field=TEXT, fact=TEXT,
            prior_value=OPTIONAL_TEXT, reported_value=OPTIONAL_TEXT, evidence_state=EVIDENCE_STATE,
            date=TEXT, source_ids=SOURCE_IDS, unresolved_questions=STRINGS)),
        open_questions=_array(QUESTION), next_stage=_enum("matcher")),
    "matcher": _object(
        comparisons=_array(_object(comparison_id=TEXT, event_id=TEXT, historical_state=TEXT,
            current_state=TEXT, relevant_policy_language=TEXT, potential_impact=TEXT,
            requirement_basis=_enum("REQUIRED_BY_SOURCE", "OPTIONAL_BROKER_SUGGESTION", "NEEDS_VERIFICATION"),
            conflict_state=CONFLICT_STATE, remaining_decision=TEXT, source_ids=SOURCE_IDS)),
        required_verification=_array(_object(verification_id=TEXT, request=TEXT, reason=TEXT,
            classification=_enum("REQUIRED_BY_SOURCE"), source_ids=SOURCE_IDS)),
        optional_suggestions=_array(_object(suggestion_id=TEXT, suggestion=TEXT, reason=TEXT,
            classification=_enum("OPTIONAL_BROKER_SUGGESTION"), source_ids=SOURCE_IDS)),
        conflicts=_array(CONFLICT), next_stage=_enum("closer")),
    "closer": _object(
        drafts=_array(_object(draft_id=TEXT, recipient_placeholder=TEXT, subject=TEXT, draft_message=TEXT,
            question_to_source_map=_array(_object(question=TEXT, reason=TEXT, source_ids=SOURCE_IDS), nonempty=True),
            open_issues=STRINGS, approval_state=_enum("UNSENT"))),
        no_draft_reason=OPTIONAL_TEXT, broker_review_checklist=STRINGS,
        approval_state=_enum("UNSENT"), next_stage=_enum("scribe")),
    "scribe": _object(
        proposed_changes=_array(_object(change_id=TEXT, entity=TEXT, field=TEXT, prior_value=OPTIONAL_TEXT,
            proposed_value=TEXT, effective_date=TEXT, evidence_state=EVIDENCE_STATE,
            source_ids=SOURCE_IDS, conflict_state=CONFLICT_STATE, reason=TEXT, write_status=_enum("UNCOMMITTED"))),
        learning_proposals=_array(_object(proposal_id=TEXT, prior_failure=TEXT, corrected_evidence=TEXT,
            scope=TEXT, proposed_rule=TEXT, regression_example=TEXT, source_ids=SOURCE_IDS,
            write_status=_enum("UNCOMMITTED"))),
        conflicts=_array(CONFLICT), approval_needed=_array(_object(proposal_id=TEXT, decision=TEXT)),
        draft_follow_up_refs=STRINGS, write_status=_enum("UNCOMMITTED")),
}


def stage_schema(stage: str) -> dict:
    """A self-contained strict JSON schema for one original agent's packet."""
    if stage not in STAGES:
        _fail("INVALID_STAGE", f"Unknown stage: {stage}")
    return deepcopy(_object(
        schema_version=_enum("broker-handoff/v1"), run_id=TEXT, case_id=TEXT, source_revision=TEXT,
        stage=_enum(stage), status=_enum("READY", "NEEDS_REVIEW", "BLOCKED"),
        reason_code={"type": ["string", "null"], "enum": [None, "SOURCE_MISSING", "HANDOFF_MISSING",
            "HANDOFF_MISMATCH", "RETRIEVAL_UNVERIFIED", "SOURCE_CONFLICT", "TOOL_ERROR"]},
        missing_inputs=STRINGS, source_refs=_array(SOURCE_REF), unknowns=_array(UNKNOWN),
        cache_event=_enum("UNKNOWN"), cache_receipt_id={"type": "null"}, payload=PAYLOAD_SCHEMAS[stage]))


def _validate_shape(value: Any, schema: dict, path: str = "packet") -> None:
    """Validate the small strict schema vocabulary we emit without dependencies."""
    expected = schema["type"]
    kinds = expected if isinstance(expected, list) else [expected]
    actual = ("null" if value is None else "boolean" if isinstance(value, bool) else
              "object" if isinstance(value, dict) else "array" if isinstance(value, list) else
              "string" if isinstance(value, str) else "number")
    if actual not in kinds:
        _fail("INVALID_PACKET", f"{path}: expected {expected}")
    if "enum" in schema and value not in schema["enum"]:
        _fail("INVALID_PACKET", f"{path}: value is outside the permitted contract")
    if actual == "object":
        properties = schema["properties"]
        if set(value) != set(properties):
            _fail("INVALID_PACKET", f"{path}: fields do not match the strict contract")
        for key, item in value.items():
            _validate_shape(item, properties[key], f"{path}.{key}")
    elif actual == "array":
        if len(value) < schema.get("minItems", 0):
            _fail("INVALID_PACKET", f"{path}: must not be empty")
        for index, item in enumerate(value):
            _validate_shape(item, schema["items"], f"{path}[{index}]")
    elif actual == "string" and (len(value) < schema.get("minLength", 0) or not value.strip()):
        _fail("INVALID_PACKET", f"{path}: must not be blank")


def _source_id_lists(value: Any):
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "source_ids":
                yield child
            else:
                yield from _source_id_lists(child)
    elif isinstance(value, list):
        for child in value:
            yield from _source_id_lists(child)


def _unique_ids(items: list, field: str, stage: str) -> set[str]:
    ids = [item[field] for item in items]
    if len(set(ids)) != len(ids):
        _fail("INVALID_PACKET", f"Duplicate {field} in {stage}", stage)
    return set(ids)


def _validate_packet(packet: dict, stage: str, controller: dict, case: dict,
                     preflight: dict, previous: list[dict]) -> None:
    _validate_shape(packet, stage_schema(stage))
    for key in ("run_id", "case_id", "source_revision"):
        if packet[key] != controller[key]:
            _fail("HANDOFF_MISMATCH", f"{stage} changed controller-owned {key}", stage)
    if packet["status"] == "BLOCKED" and packet["reason_code"] is None:
        _fail("INVALID_PACKET", "BLOCKED requires a reason_code", stage)
    sources = {source["source_id"]: source for source in case["sources"]}
    cited = set()
    for ref in packet["source_refs"]:
        source = sources.get(ref["source_id"])
        if source is None:
            _fail("INVALID_CITATION", f"{stage} cited an unknown source", stage)
        for field, original in (("source_type", "source_type"), ("document_date", "recorded_at"),
                                ("effective_from", "effective_from"), ("effective_to", "effective_to")):
            expected = source.get(original) if source.get(original) is not None else "UNKNOWN"
            if ref[field] != expected:
                _fail("INVALID_CITATION", f"{stage} changed source {field}", stage)
        original = source["sections"].get(ref["section_or_locator"])
        if original is None or ref["excerpt"] not in original:
            _fail("INVALID_CITATION", f"{stage} quotation does not match its original section", stage)
        cited.add(ref["source_id"])
    for ids in _source_id_lists({"payload": packet["payload"], "unknowns": packet["unknowns"]}):
        if any(sid not in sources or sid not in cited for sid in ids):
            _fail("INVALID_CITATION", f"{stage} material claim lacks a validated source reference", stage)
    if packet["status"] == "BLOCKED":
        return
    if not cited:
        _fail("INVALID_CITATION", f"{stage} needs original evidence", stage)
    unknown_fields = _unique_ids(packet["unknowns"], "field", stage)
    if not {item["field"] for item in preflight["unknowns"]}.issubset(unknown_fields):
        _fail("UNKNOWN_DROPPED", f"{stage} omitted a known unresolved field", stage)
    payload = packet["payload"]
    if stage == "watcher":
        _unique_ids(payload["events"], "event_id", stage)
        _unique_ids(payload["open_questions"], "question_id", stage)
    elif stage == "matcher":
        events = {event["event_id"] for event in previous[0]["payload"]["events"]}
        _unique_ids(payload["comparisons"], "comparison_id", stage)
        compared = {item["event_id"] for item in payload["comparisons"]}
        if compared != events:
            _fail("HANDOFF_MISMATCH", "Matcher must address each original Watcher event without inventing events", stage)
        if (payload["conflicts"] or any(item["conflict_state"] == "UNRESOLVED" for item in payload["comparisons"])) and packet["status"] != "NEEDS_REVIEW":
            _fail("INVALID_PACKET", "Material conflicts require NEEDS_REVIEW", stage)
    elif stage == "closer":
        _unique_ids(payload["drafts"], "draft_id", stage)
        if not payload["drafts"] and payload["no_draft_reason"] is None:
            _fail("INVALID_PACKET", "An empty draft list needs an explanation", stage)
        if not payload["broker_review_checklist"]:
            _fail("INVALID_PACKET", "Closer must preserve broker review", stage)
    else:
        changes = _unique_ids(payload["proposed_changes"], "change_id", stage)
        learnings = _unique_ids(payload["learning_proposals"], "proposal_id", stage)
        drafts = {item["draft_id"] for item in previous[2]["payload"]["drafts"]}
        if set(payload["draft_follow_up_refs"]) != drafts:
            _fail("HANDOFF_MISMATCH", "Scribe must retain exactly the drafts Closer produced", stage)
        approvals = {item["proposal_id"] for item in payload["approval_needed"]}
        if not (changes | learnings | drafts).issubset(approvals):
            _fail("INVALID_PACKET", "Every change, learning proposal and draft needs a broker decision", stage)
        if previous[1]["payload"]["conflicts"] and not payload["conflicts"]:
            _fail("UNKNOWN_DROPPED", "Scribe cannot drop Matcher's unresolved conflicts", stage)
        disputed_sources = {sid for conflict in previous[1]["payload"]["conflicts"] for sid in conflict["source_ids"]}
        disputed_sources.update(sid for comparison in previous[1]["payload"]["comparisons"]
                                if comparison["conflict_state"] == "UNRESOLVED" for sid in comparison["source_ids"])
        for change in payload["proposed_changes"]:
            if disputed_sources.intersection(change["source_ids"]) and change["conflict_state"] != "UNRESOLVED":
                _fail("UNKNOWN_DROPPED", "Scribe cannot mark a proposal based on disputed sources as conflict-free", stage)
        if (payload["conflicts"] or any(c["conflict_state"] == "UNRESOLVED" for c in payload["proposed_changes"])) and packet["status"] != "NEEDS_REVIEW":
            _fail("INVALID_PACKET", "Disputed proposals require NEEDS_REVIEW", stage)


def run_pipeline(case: dict, preflight: dict,
                 invoke: Callable[[str, str, dict], dict],
                 on_stage: Callable[[str, dict], None] | None = None) -> dict:
    """Invoke Watcher, Matcher, Closer, Scribe once each, stopping on BLOCKED.

    ``invoke(stage, prompt, schema)`` is the external model adapter; the controller
    never substitutes canned stage output. Exceptions reject the run instead of
    silently producing a demo. ``on_stage(stage, validated_packet)`` observes
    accepted output. Inputs and callback packets are copied to prevent mutation.
    """
    if not isinstance(case, dict) or not isinstance(preflight, dict):
        _fail("INVALID_INPUT", "Case and deterministic preflight must be objects")
    case, preflight = deepcopy(case), deepcopy(preflight)
    revision = hashlib.sha256(json.dumps(case, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    if preflight.get("schema_version") != "broker-review/v1" or preflight.get("source_revision") != revision:
        _fail("HANDOFF_MISMATCH", "Preflight does not belong to this source revision")
    if any(preflight.get(key) != case.get(key) for key in ("case_id", "client_id", "policy_id")):
        _fail("HANDOFF_MISMATCH", "Preflight and selected case identities differ")
    controller = {"run_id": str(uuid4()), "case_id": case["case_id"], "source_revision": revision,
                  "contract_version": "broker-handoff/v1"}
    started_at = datetime.now(timezone.utc).isoformat()
    previous: list[dict] = []
    completed: list[str] = []
    contract = (PROMPTS / "contract.md").read_text()
    for stage in STAGES:
        context = {"controller": {**controller, "stage": stage, "completed_stages": list(completed)},
                   "case": case, "preflight": preflight, "previous_packets": previous}
        prompt = contract + "\n\n" + (PROMPTS / f"{stage}.md").read_text()
        prompt += "\n\nCASE_CONTEXT_JSON\n" + json.dumps(context, ensure_ascii=False)
        try:
            packet = invoke(stage, prompt, stage_schema(stage))
            _validate_packet(packet, stage, controller, case, preflight, previous)
        except PipelineError as exc:
            if exc.stage is None:
                exc.stage = stage
            raise
        except Exception as exc:
            # Do not expose model/provider logs or case content in adapter errors.
            raise PipelineError("TOOL_ERROR", f"{stage} model invocation failed; no handoff accepted", stage) from exc
        packet = deepcopy(packet)
        previous.append(packet)
        if packet["status"] != "BLOCKED":
            completed.append(stage)
        if on_stage is not None:
            on_stage(stage, deepcopy(packet))
        if packet["status"] == "BLOCKED":
            break
    blocked = previous[-1]["status"] == "BLOCKED"
    return {
        "schema_version": "broker-agent-run/v1", **controller, "created_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "status": "BLOCKED" if blocked else "NEEDS_BROKER_REVIEW",
        "blocked_stage": previous[-1]["stage"] if blocked else None,
        "completed_stages": completed, "stages": previous, "preflight": preflight,
        "execution": {"kind": "four_agent_pipeline", "model_calls": len(previous), "completed_stages": list(completed)},
        "actions": {"messages_sent": 0, "records_updated": 0},
        "validation": "Strict handoffs, source identities, quotations and source dates validated; interpretations require broker review.",
    }
