"""Deterministic in-app updates from recorded runs and observed file changes."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile


def source_ids(value) -> list[str]:
    found = set()
    def visit(node):
        if isinstance(node, dict):
            for key, child in node.items():
                if key == "source_id" and isinstance(child, str):
                    found.add(child)
                elif key == "source_ids" and isinstance(child, list):
                    found.update(item for item in child if isinstance(item, str))
                else:
                    visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)
    visit(value)
    return sorted(found)


def job_updates(job: dict) -> list[dict]:
    if job.get("status") not in {"completed", "failed"}:
        return []
    packets = {item["stage"]: item["packet"] for item in job.get("stages", [])}
    result = job.get("result") or {}
    timestamp = result.get("finished_at") or job.get("updated_at") or job["created_at"]
    items = []
    def add(kind, title, summary, stage, evidence, action, created_at=timestamp):
        items.append({"id": job["id"] + "--" + kind, "kind": kind, "title": title,
                      "summary": summary, "created_at": created_at, "run_id": job["id"],
                      "stage": stage, "source_ids": source_ids(evidence), "status": "unread",
                      "snoozed_until": None, "action_label": action})
    matcher = packets.get("matcher", {})
    payload = matcher.get("payload", {})
    conflicts = payload.get("conflicts", [])
    required = payload.get("required_verification", [])
    questions = result.get("preflight", {}).get("review_questions", [])
    if job["status"] == "failed":
        add("needs_review", "Review stopped", "This recorded run did not complete. Review its saved handoffs before trying again.",
            job.get("current_stage"), list(packets.values()), "Review activity")
    elif conflicts or required or questions or result.get("status") == "NEEDS_BROKER_REVIEW":
        preflight = result.get("preflight", {})
        missing = [item["name"] for item in preflight.get("reported_assignments", [])
                   if item.get("listed_in_current_supplied_schedule") is False]
        review_date = preflight.get("review_at", "")[:10]
        revision = (preflight.get("current_schedule") or {}).get("revision", "current")
        if missing:
            names = missing[0] if len(missing) == 1 else ", ".join(missing[:-1]) + " and " + missing[-1]
            verb = "is" if len(missing) == 1 else "are"
            summary = f"{names} {verb} missing from the supplied {revision} schedule. Please check their reported starts against the supplied evidence."
        elif questions:
            summary = questions[0]["question"]
        else:
            summary = "The recorded source evidence needs your review before the next decision."
        if review_date:
            summary += f" Review date: {review_date}."
        add("needs_review", "Evidence needs your review", summary, "matcher", matcher, "Review evidence")
    drafts = packets.get("closer", {}).get("payload", {}).get("drafts", [])
    if drafts:
        noun = "draft" if len(drafts) == 1 else "drafts"
        add("drafts_ready", "Drafts ready to review", f"I prepared {len(drafts)} follow-up {noun} for your review. Nothing has been sent.",
            "closer", drafts, "Review drafts")
    changes = packets.get("scribe", {}).get("payload", {}).get("proposed_changes", [])
    if changes:
        noun, verb = ("update", "is") if len(changes) == 1 else ("updates", "are")
        add("record_proposals", "Record proposals ready", f"{len(changes)} proposed {noun} {verb} ready for your review. No customer records were changed.",
            "scribe", changes, "Review proposals")
    approval = job.get("approval")
    if approval:
        decision = "approved" if approval["decision"] == "approve" else "rejected"
        add("review_recorded", "Review decision recorded", f"The packet was {decision}. This is an audit decision; no messages were sent or customer records changed.",
            None, list(packets.values()), "View decision", approval["decided_at"])
    return items


def latest_job_updates(jobs) -> list[dict]:
    """Collapse repeated reviews of the same case and evidence revision."""
    groups = {}
    for job in jobs:
        result = job.get("result") or {}
        # The revision hashes the complete case, including its identity. Older
        # failed jobs may not yet have produced a packet containing case_id.
        key = job.get("source_revision") or result.get("source_revision")
        groups.setdefault(key, []).append(job)
    visible = []
    for values in groups.values():
        completed = [job for job in values if job.get("status") == "completed"]
        newest = max(completed, key=lambda job: job["created_at"], default=None)
        if newest:
            visible.extend(job_updates(newest))
        failed = [job for job in values if job.get("status") == "failed"
                  and (newest is None or job["created_at"] > newest["created_at"])]
        if failed:
            failure = max(failed, key=lambda job: job["created_at"])
            visible.extend(item for item in job_updates(failure) if item["kind"] == "needs_review")
    return visible


class UpdateStore:
    """Caller holds the application's shared lock for every operation."""
    def __init__(self, path: Path, now=None):
        self.path = Path(path)
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.items = {}
        self.monitor = {"enabled": True, "interval_seconds": 30, "last_checked_at": None,
                        "last_changed_at": None, "source_label": "Demo case files", "error": None}
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(saved.get("items"), dict):
                self.items = saved["items"]
            if type(saved.get("monitor", {}).get("enabled")) is bool:
                self.monitor["enabled"] = saved["monitor"]["enabled"]
            self.monitor["last_changed_at"] = saved.get("monitor", {}).get("last_changed_at")
        except (OSError, ValueError, TypeError, AttributeError):
            pass

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                             prefix=".updates-", delete=False) as handle:
                temporary = Path(handle.name)
                json.dump({"items": self.items, "monitor": self.monitor}, handle, ensure_ascii=False, allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()

    def upsert(self, item):
        previous = self.items.get(item["id"])
        updated = deepcopy(item)
        if previous:
            # Re-reading an unchanged run must not make it unread again.
            if not (item["kind"] in {"review_recorded", "source_changed"} and previous["created_at"] != item["created_at"]):
                updated["status"] = previous["status"]
                updated["snoozed_until"] = previous["snoozed_until"]
        if updated != previous:
            self.items[item["id"]] = updated
            return True
        return False

    def feed(self, jobs) -> dict:
        derived = latest_job_updates(jobs)
        visible_ids = {item["id"] for item in derived}
        changed = False
        for item in derived:
            changed = self.upsert(item) or changed
        now = self.now()
        for item in self.items.values():
            if item["kind"] == "source_changed":
                visible_ids.add(item["id"])
            if item["status"] == "snoozed" and datetime.fromisoformat(item["snoozed_until"]) <= now:
                item.update(status="unread", snoozed_until=None)
                changed = True
        if changed:
            self.save()
        visible = [deepcopy(self.items[key]) for key in visible_ids]
        visible.sort(key=lambda item: (item["created_at"], item["id"]), reverse=True)
        return {"items": visible, "unread_count": sum(item["status"] == "unread" for item in visible),
                "monitor": deepcopy(self.monitor)}

    def act(self, item_id: str, action: str):
        if item_id not in self.items:
            raise KeyError(item_id)
        if action not in {"read", "unread", "snooze"}:
            raise ValueError("Unknown update action")
        item = self.items[item_id]
        item["status"] = "snoozed" if action == "snooze" else action
        item["snoozed_until"] = (self.now() + timedelta(hours=1)).isoformat() if action == "snooze" else None
        self.save()
