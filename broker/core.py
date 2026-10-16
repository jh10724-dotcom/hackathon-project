"""Deterministic preflight over explicitly normalized, fictional source records.

This module performs no model inference, retrieval, sending, or record writes.
The small roster/date grammar is a fixture validation boundary, not a general
insurance-document parser. A future model can propose normalized records; those
records still need their original sources and must pass these checks.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime
import hashlib
import json
import re
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class CaseValidationError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _fail(code: str, message: str) -> None:
    raise CaseValidationError(code, message)


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail("INVALID_INPUT", f"{label} must be a nonempty string")
    return value


def _timestamp(value: Any, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(_required_string(value, label).replace("Z", "+00:00"))
    except ValueError:
        _fail("INVALID_DATE", f"{label} must be an ISO timestamp with a UTC offset")
    if parsed.tzinfo is None:
        _fail("INVALID_DATE", f"{label} must include a UTC offset")
    return parsed


def _list(case: dict, key: str) -> list:
    value = case.get(key)
    if not isinstance(value, list):
        _fail("INVALID_INPUT", f"{key} must be an array")
    return value


def _recorded_date(value: Any) -> date:
    text = _required_string(value, "recorded_at")
    if "T" in text:
        return _timestamp(text, "recorded_at").date()
    try:
        return date.fromisoformat(text)
    except ValueError:
        _fail("INVALID_DATE", "recorded_at must be an ISO date or timestamp")


def _source_map(case: dict) -> dict[str, dict]:
    sources: dict[str, dict] = {}
    for source in _list(case, "sources"):
        if not isinstance(source, dict):
            _fail("INVALID_INPUT", "Every source must be an object")
        sid = _required_string(source.get("source_id"), "source_id")
        if sid in sources:
            _fail("DUPLICATE_SOURCE", f"Source {sid} occurs more than once")
        if source.get("client_id") != case["client_id"]:
            _fail("SCOPE_MISMATCH", f"Source {sid} belongs to another client")
        if source.get("policy_id") not in (None, case["policy_id"]):
            _fail("SCOPE_MISMATCH", f"Source {sid} belongs to another policy")
        _recorded_date(source.get("recorded_at"))
        _required_string(source.get("source_type"), f"{sid}.source_type")
        sections = source.get("sections")
        if not isinstance(sections, dict) or not sections:
            _fail("INVALID_INPUT", f"Source {sid} needs original sections")
        for locator, text in sections.items():
            if not isinstance(locator, str) or not locator.startswith(sid + "#"):
                _fail("INVALID_CITATION", f"Section {locator!r} does not belong to {sid}")
            _required_string(text, f"{sid} section text")
        sources[sid] = source
    if not sources:
        _fail("SOURCE_MISSING", "At least one original source is required")
    return sources


def _evidence(ref: Any, sources: dict[str, dict]) -> tuple[dict, dict]:
    if not isinstance(ref, dict):
        _fail("INVALID_CITATION", "Every normalized record needs a source_ref")
    sid, locator, quote = (ref.get(k) for k in ("source_id", "locator", "quote"))
    source = sources.get(sid) if isinstance(sid, str) else None
    text = source["sections"].get(locator) if source and isinstance(locator, str) else None
    if not isinstance(quote, str) or not quote.strip() or text is None or quote not in text:
        _fail("INVALID_CITATION", f"Citation {sid}/{locator} is not an exact original passage")
    return source, {"source_id": sid, "locator": locator, "quote": quote}


def _roster(record: dict, source: dict, ref: dict, case: dict) -> dict:
    if source["source_type"] not in {"policy_fixture", "issued_endorsement_fixture"}:
        _fail("SOURCE_AUTHORITY", "Only a supplied policy or issued endorsement can define a schedule")
    if source.get("policy_id") != case["policy_id"]:
        _fail("SCOPE_MISMATCH", "Schedule source needs the selected policy ID")
    start = _timestamp(source.get("effective_from"), "schedule effective_from")
    end = _timestamp(source.get("effective_to"), "schedule effective_to")
    if end < start:
        _fail("INVALID_DATE", "Schedule effective period ends before it starts")
    revision = _required_string(record.get("revision"), "schedule revision")
    quote = ref["quote"]
    prefixes = (f"Driver schedule {revision}:", f"{revision} lists:")
    prefix = next((p for p in prefixes if quote.startswith(p)), None)
    if prefix is None:
        _fail("UNSUPPORTED_ROSTER", "Roster quote must begin at its explicit positive schedule list")
    rows = [v.strip().removeprefix("- ").rstrip(".") for v in re.split(r"[;\n]", quote[len(prefix):]) if v.strip()]
    extracted = []
    for row in rows:
        match = re.fullmatch(r"([A-Z0-9]+-D\d+) ([A-Za-z][A-Za-z '’-]+)", row)
        if not match:
            _fail("UNSUPPORTED_ROSTER", "Roster excerpt contains text outside the supported driver-list grammar")
        extracted.append({"driver_id": match[1], "name": match[2]})
    if not extracted or len({d["driver_id"] for d in extracted}) != len(extracted):
        _fail("INVALID_ROSTER", "Roster must contain distinct drivers")
    if record.get("drivers") != extracted:
        _fail("UNSUPPORTED_CLAIM", "Normalized schedule drivers differ from the cited positive roster")
    return {"revision": revision, "drivers": extracted, "effective_from": source["effective_from"],
            "effective_to": source["effective_to"], "recorded_at": source["recorded_at"],
            "source_ref": ref, "evidence_state": "DOCUMENT_CONFIRMED"}


def _assignment(record: dict, source: dict, ref: dict, review: datetime) -> dict:
    if source["source_type"] not in {"client_message", "client_clarification"}:
        _fail("SOURCE_AUTHORITY", "Reported assignment dates need a client source")
    did = _required_string(record.get("driver_id"), "assignment driver_id")
    name = _required_string(record.get("name"), "assignment name")
    if f"{name} ({did})" not in ref["quote"]:
        _fail("UNSUPPORTED_CLAIM", "Assignment name and ID are not together in the cited passage")
    try:
        start = date.fromisoformat(_required_string(record.get("reported_start_date"), "reported_start_date"))
    except ValueError:
        _fail("INVALID_DATE", "reported_start_date must be an ISO date")
    # The limited fixture grammar binds the normalized date to the actual date
    # expression and derives a missing year from the dated source, never now().
    match = re.search(r"(?:starting today, |start driving )([A-Z][a-z]+ \d{1,2})(?:, (\d{4}))?\.", ref["quote"])
    if not match:
        _fail("UNSUPPORTED_ASSIGNMENT", "Assignment quote needs its explicit start-date sentence")
    year = int(match[2]) if match[2] else _recorded_date(source["recorded_at"]).year
    try:
        cited_date = datetime.strptime(f"{match[1]} {year}", "%B %d %Y").date()
    except ValueError:
        _fail("INVALID_DATE", "Assignment source has an invalid calendar date")
    if start != cited_date:
        _fail("UNSUPPORTED_CLAIM", "Normalized assignment date differs from the cited source date")
    return {"driver_id": did, "name": name, "reported_start_date": start.isoformat(),
            "timing": "START_DATE_REACHED" if start <= review.date() else "PLANNED",
            "evidence_state": "CLIENT_REPORTED", "source_ref": ref}


def analyze_case(case: dict, review_at: str | None = None) -> dict:
    """Return review facts/questions, or raise CaseValidationError before output.

    review_at selects effective dates using all supplied records. It is not a
    knowledge/receipt cutoff and does not claim the record was known then.
    """
    if not isinstance(case, dict) or case.get("schema_version") != "broker-case/v1":
        _fail("INVALID_INPUT", "Expected broker-case/v1")
    if case.get("fictional") is not True:
        _fail("DEMO_ONLY", "This preflight accepts explicitly fictional cases only")
    for key in ("case_id", "client_id", "client_name", "policy_id"):
        _required_string(case.get(key), key)
    review_text = review_at if review_at is not None else case.get("review_at")
    review = _timestamp(review_text, "review_at")
    try:
        review = review.astimezone(ZoneInfo(_required_string(case.get("review_timezone"), "review_timezone")))
    except ZoneInfoNotFoundError:
        _fail("INVALID_DATE", "review_timezone must be an IANA timezone")
    sources = _source_map(case)
    refs: dict[tuple[str, str, str], dict] = {}

    def resolve(record: Any) -> tuple[dict, dict]:
        if not isinstance(record, dict):
            _fail("INVALID_INPUT", "Every normalized record must be an object")
        source, ref = _evidence(record.get("source_ref"), sources)
        refs[(ref["source_id"], ref["locator"], ref["quote"])] = ref
        return source, ref

    schedules = [_roster(r, *resolve(r), case) for r in _list(case, "schedule_records")]
    schedules.sort(key=lambda s: _timestamp(s["effective_from"], "effective_from"))
    active = [s for s in schedules if _timestamp(s["effective_from"], "effective_from") <= review <= _timestamp(s["effective_to"], "effective_to")]
    current = deepcopy(active[-1]) if active else None
    if current:
        tied = [s for s in active if _timestamp(s["effective_from"], "effective_from") == _timestamp(current["effective_from"], "effective_from")]
        if len(tied) > 1:
            _fail("SCHEDULE_CONFLICT", "Multiple supplied schedules share the selected effective start; broker review is required")
    assignments = [_assignment(r, *resolve(r), review) for r in _list(case, "assignment_records")]
    if len({a["driver_id"] for a in assignments}) != len(assignments):
        _fail("ASSIGNMENT_CONFLICT", "A driver has multiple normalized start-date records; resolve the source conflict first")
    unknowns = []
    allowed_unknowns = {"vehicle_assignments": "vehicle assignments", "premium": "premium", "limits": "limits", "coverage_determination": "coverage determination"}
    for r in _list(case, "unknown_records"):
        _, ref = resolve(r)
        field = r.get("field")
        phrase = allowed_unknowns.get(field)
        quote = ref["quote"].lower()
        if not phrase or phrase not in quote or not any(s in quote for s in ("does not provide", "no vehicle assignments", "not provided their vehicle assignments")):
            _fail("UNSUPPORTED_CLAIM", "An unknown field must cite an explicit not-supplied statement")
        unknowns.append({"field": field, "state": "NOT_SUPPLIED_IN_RECORDS", "source_ref": ref})
    questions = []
    if not current:
        questions.append({"id": "applicable-schedule", "question": "Which issued driver schedule applies on the selected review date?", "source_refs": [], "reason": "No supplied schedule has an applicable effective period."})
    roster_ids = {d["driver_id"] for d in current["drivers"]} if current else set()
    for assignment in assignments:
        assignment["listed_in_current_supplied_schedule"] = assignment["driver_id"] in roster_ids if current else None
        if current and assignment["driver_id"] not in roster_ids:
            questions.append({"id": f"schedule-{assignment['driver_id']}", "question": f"What issued schedule evidence should the broker review for {assignment['name']}'s reported {assignment['reported_start_date']} start?", "source_refs": [assignment["source_ref"], current["source_ref"]], "reason": "The driver is not listed in the current supplied schedule; this does not establish coverage status."})
    vehicle_unknown = next((u for u in unknowns if u["field"] == "vehicle_assignments"), None)
    if vehicle_unknown and assignments:
        questions.append({"id": "vehicle-assignments", "question": "Which vehicle is each newly reported driver assigned to?", "source_refs": [vehicle_unknown["source_ref"]], "reason": "The supplied records explicitly leave vehicle assignments open."})
    digest = hashlib.sha256(json.dumps(case, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    return {"schema_version": "broker-review/v1", "case_id": case["case_id"], "client_id": case["client_id"],
            "client_name": case["client_name"], "policy_id": case["policy_id"], "fictional": True,
            "status": "NEEDS_REVIEW", "review_at": review_text, "source_revision": digest,
            "execution": {"kind": "deterministic_preflight", "model_called": False},
            "time_basis": "Effective-date review using all supplied evidence; receipt time is not a knowledge cutoff.",
            "current_schedule": current, "historical_schedules": [s for s in schedules if current and _timestamp(s["effective_from"], "effective_from") < _timestamp(current["effective_from"], "effective_from")],
            "reported_assignments": assignments, "unknowns": unknowns, "review_questions": questions,
            "evidence_refs": list(refs.values()), "human_decisions": ["Review cited source passages and unresolved questions.", "Approve any proposed follow-up or client-record change separately."],
            "action_state": {"messages_sent": False, "records_changed": False, "coverage_determined": False},
            "warnings": ["Fictional source records; no insurance coverage determination.", "Client-reported start dates do not establish observed driving or vehicle assignment."]}
