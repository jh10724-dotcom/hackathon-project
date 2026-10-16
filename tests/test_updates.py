"""Deterministic feed/monitor tests; fake model only, no network or paid calls."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock
from uuid import uuid4

from broker.server import ApiError, BrokerApplication
from broker.updates import UpdateStore
from test_server import FIXTURE, fake_packet


def recorded_job(created="2026-10-16T12:00:00+00:00", drafts=2, changes=5):
    return {"id": str(uuid4()), "created_at": created, "updated_at": created,
            "source_revision": "same-case-revision", "status": "completed", "approval": None,
            "stages": [
                {"stage": "matcher", "packet": {"payload": {"conflicts": [{"source_ids": ["S1"]}],
                    "required_verification": []}}},
                {"stage": "closer", "packet": {"payload": {"drafts": [
                    {"draft_id": str(i), "source_ids": ["S1"]} for i in range(drafts)]}}},
                {"stage": "scribe", "packet": {"payload": {"proposed_changes": [
                    {"change_id": str(i), "source_ids": ["S2"]} for i in range(changes)]}}}],
            "result": {"case_id": "case", "status": "NEEDS_BROKER_REVIEW", "finished_at": created}}


class FeedTests(unittest.TestCase):
    def test_dynamic_counts_dedupe_and_state_survive_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "updates.json"
            store = UpdateStore(path)
            older = recorded_job(changes=6)
            latest = recorded_job("2026-10-16T13:00:00+00:00", changes=5)
            jobs = [older, latest]
            feed = store.feed(jobs)
            self.assertEqual(len(feed["items"]), 3)
            self.assertEqual({item["run_id"] for item in feed["items"]}, {latest["id"]})
            proposals = next(item for item in feed["items"] if item["kind"] == "record_proposals")
            self.assertTrue(proposals["summary"].startswith("5 proposed"))
            self.assertEqual(proposals["source_ids"], ["S2"])
            store.act(proposals["id"], "read")
            reloaded = UpdateStore(path)
            repeated = reloaded.feed(jobs)
            self.assertEqual(len(repeated["items"]), 3)
            self.assertEqual(repeated["unread_count"], 2)
            self.assertEqual(next(item for item in repeated["items"] if item["id"] == proposals["id"])["status"], "read")

    def test_snooze_is_one_hour_and_expiry_becomes_unread(self):
        with tempfile.TemporaryDirectory() as directory:
            current = [datetime(2026, 10, 16, tzinfo=timezone.utc)]
            store = UpdateStore(Path(directory) / "updates.json", now=lambda: current[0])
            job = recorded_job()
            item = store.feed([job])["items"][0]
            store.act(item["id"], "snooze")
            snoozed = next(value for value in store.feed([job])["items"] if value["id"] == item["id"])
            self.assertEqual(datetime.fromisoformat(snoozed["snoozed_until"]), current[0] + timedelta(hours=1))
            self.assertEqual(snoozed["status"], "snoozed")
            current[0] += timedelta(hours=1)
            resumed = next(value for value in store.feed([job])["items"] if value["id"] == item["id"])
            self.assertEqual(resumed["status"], "unread")
            self.assertIsNone(resumed["snoozed_until"])

    def test_review_recorded_is_audited_without_action_claims_and_new_decision_is_unread(self):
        with tempfile.TemporaryDirectory() as directory:
            store = UpdateStore(Path(directory) / "updates.json")
            job = recorded_job(drafts=0, changes=0)
            self.assertEqual([item["kind"] for item in store.feed([job])["items"]], ["needs_review"])
            job["approval"] = {"decision": "approve", "decided_at": "2026-10-16T14:00:00+00:00", "note": ""}
            item = next(item for item in store.feed([job])["items"] if item["kind"] == "review_recorded")
            self.assertIn("audit decision", item["summary"])
            store.act(item["id"], "read")
            job["approval"].update(decision="reject", decided_at="2026-10-16T15:00:00+00:00")
            changed = next(value for value in store.feed([job])["items"] if value["id"] == item["id"])
            self.assertEqual(changed["status"], "unread")
            self.assertIn("rejected", changed["summary"])


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.case_path = self.root / "case.json"
        self.original = json.loads(FIXTURE.read_text())
        self.case_path.write_text(json.dumps(self.original))
        self.invoke = Mock(side_effect=fake_packet)
        self.ready = Mock(return_value=True)
        self.app = BrokerApplication(case_path=self.case_path, runs_dir=self.root / "runs",
                                     invoke=self.invoke, readiness=self.ready)

    def tearDown(self):
        self.app.stop_monitor()
        if self.app.worker:
            self.app.worker.join(3)
        self.temp.cleanup()

    def changed_case(self):
        changed = deepcopy(self.original)
        source = changed["sources"][0]
        locator = next(iter(source["sections"]))
        source["sections"][locator] += "\nAdditional supplied source text for a file-change test."
        self.case_path.write_text(json.dumps(changed))
        return changed, source["source_id"]

    def test_actual_change_detected_once_invalid_change_retains_last_valid_case(self):
        self.assertIsNone(self.app._monitor_thread)
        for _ in range(2):
            self.assertEqual(self.app.check_updates()["items"], [])
        changed, sid = self.changed_case()
        feed = self.app.check_updates()
        self.assertEqual(len(feed["items"]), 1)
        self.assertEqual(feed["items"][0]["source_ids"], [sid])
        self.assertIn("evidence was detected", feed["items"][0]["summary"])
        self.assertEqual(self.app.case, changed)
        self.assertEqual(self.app.check_updates()["items"], feed["items"])
        self.case_path.write_text("{invalid")
        invalid = self.app.check_updates()
        self.assertIsNotNone(invalid["monitor"]["error"])
        self.assertEqual(self.app.case, changed)
        self.assertEqual(len(invalid["items"]), 1)
        self.invoke.assert_not_called()
        self.ready.assert_not_called()

    def test_monitor_enabled_setting_persists_without_starting_background_thread(self):
        self.app.set_monitor(False)
        other = BrokerApplication(case_path=self.case_path, runs_dir=self.root / "runs", invoke=self.invoke, readiness=self.ready)
        self.assertFalse(other.updates_response()["monitor"]["enabled"])
        self.assertIsNone(other._monitor_thread)
        self.invoke.assert_not_called()

    def test_reobserved_source_transition_becomes_unread_again(self):
        changed, _ = self.changed_case()
        first = self.app.check_updates()["items"][0]
        self.app.update_action(first["id"], "read")
        self.case_path.write_text(json.dumps(self.original))
        self.app.check_updates()
        self.case_path.write_text(json.dumps(changed))
        repeated = next(item for item in self.app.check_updates()["items"] if item["id"] == first["id"])
        self.assertEqual(repeated["status"], "unread")
        self.assertNotEqual(first["created_at"], repeated["created_at"])
        self.invoke.assert_not_called()

    def test_paused_monitor_does_not_allow_stale_approval_or_invalid_new_runs(self):
        job_id = self.app.start_run()
        self.app.worker.join(3)
        self.app.set_monitor(False)
        self.changed_case()
        with self.assertRaises(ApiError) as stale:
            self.app.approve(job_id, "approve", "")
        self.assertEqual(stale.exception.status, 409)
        calls = self.invoke.call_count
        self.case_path.write_text("{invalid")
        for operation in (lambda: self.app.approve(job_id, "approve", ""), self.app.start_run):
            with self.assertRaises(ApiError) as invalid:
                operation()
            self.assertEqual(invalid.exception.status, 409)
        self.assertEqual(self.invoke.call_count, calls)

    def test_run_snapshot_stays_immutable_and_stale_approval_is_rejected(self):
        entered, release = threading.Event(), threading.Event()
        seen = []
        def invoke(stage, prompt, schema):
            context = json.loads(prompt.split("\n\nCASE_CONTEXT_JSON\n", 1)[1])
            seen.append(context["case"])
            if stage == "watcher":
                entered.set()
                if not release.wait(3):
                    raise RuntimeError("test timeout")
            return fake_packet(stage, prompt, schema)
        self.app.invoke = invoke
        job_id = self.app.start_run()
        try:
            self.assertTrue(entered.wait(2))
            self.changed_case()
            self.app.check_updates()
        finally:
            release.set()
        self.app.worker.join(3)
        job = self.app.get_job(job_id)
        self.assertEqual(job["status"], "completed", job["error"])
        self.assertEqual(len(seen), 4)
        self.assertTrue(all(case == self.original for case in seen))
        self.assertNotEqual(job["source_revision"], self.app.preflight["source_revision"])
        with self.assertRaises(ApiError) as error:
            self.app.approve(job_id, "approve", "")
        self.assertEqual(error.exception.status, 409)
        self.assertIsNone(self.app.get_job(job_id)["approval"])


if __name__ == "__main__":
    unittest.main()
