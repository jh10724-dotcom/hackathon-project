"""Real local HTTP tests with fake inference. No model calls or cloud requests."""
from copy import deepcopy
import http.client
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from broker.pipeline import STAGES
from broker.server import BrokerApplication, BrokerHTTPServer, MAX_BODY_BYTES, codex_logged_in


FIXTURE = Path(__file__).resolve().parents[1] / "data/ap001.json"


def fake_packet(stage, prompt, schema):
    context = json.loads(prompt.split("\n\nCASE_CONTEXT_JSON\n", 1)[1])
    controller, case = context["controller"], context["case"]
    refs = []
    for source in case["sources"]:
        locator, text = next(iter(source["sections"].items()))
        refs.append({"source_id": source["source_id"], "source_type": source["source_type"],
                     "section_or_locator": locator, "document_date": source["recorded_at"],
                     "effective_from": source.get("effective_from") or "UNKNOWN",
                     "effective_to": source.get("effective_to") or "UNKNOWN", "excerpt": text})
    payloads = {
        "watcher": {"events": [], "open_questions": [], "next_stage": "matcher"},
        "matcher": {"comparisons": [], "required_verification": [], "optional_suggestions": [],
                    "conflicts": [], "next_stage": "closer"},
        "closer": {"drafts": [], "no_draft_reason": "Testing stage transport only.",
                   "broker_review_checklist": ["Review original records."], "approval_state": "UNSENT", "next_stage": "scribe"},
        "scribe": {"proposed_changes": [], "learning_proposals": [], "conflicts": [],
                   "approval_needed": [], "draft_follow_up_refs": [], "write_status": "UNCOMMITTED"},
    }
    return {"schema_version": "broker-handoff/v1", "run_id": controller["run_id"], "case_id": case["case_id"],
            "source_revision": controller["source_revision"], "stage": stage, "status": "NEEDS_REVIEW",
            "reason_code": None, "missing_inputs": [], "source_refs": refs,
            "unknowns": [{"field": item["field"], "description": "Not supplied.",
                          "source_ids": [item["source_ref"]["source_id"]]} for item in context["preflight"]["unknowns"]],
            "cache_event": "UNKNOWN", "cache_receipt_id": None, "payload": payloads[stage]}


class HTTPTests(unittest.TestCase):
    def test_explicit_public_origin_handles_proxy_host_without_trusting_arbitrary_origins(self):
        self.app.public_origin = "https://broker.example.test"
        path = "/api/runs/00000000-0000-4000-8000-000000000000/approval"
        body = {"decision": "reject", "note": "Transport check only"}
        status, _, _ = self.request("POST", path, body, {"Origin": self.app.public_origin})
        self.assertEqual(status, 404)  # Passed origin validation; no such job.
        status, _, _ = self.request("POST", path, body, {"Origin": "https://untrusted.example.test"})
        self.assertEqual(status, 403)
        self.invoke.assert_not_called()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.web = self.root / "web"
        self.web.mkdir()
        (self.web / "index.html").write_text("<!doctype html><title>Broker</title>")
        (self.web / "app.js").write_text("window.ready = true;")
        (self.web / ".env").write_text("MUST_NOT_LEAK=secret")
        (self.root / "secret.txt").write_text("outside source")
        (self.web / "outside.txt").symlink_to(self.root / "secret.txt")
        self.ready = Mock(return_value=True)
        self.invoke = Mock(side_effect=fake_packet)
        self.app = BrokerApplication(case_path=FIXTURE, web_root=self.web, runs_dir=self.root / "runs",
                                     invoke=self.invoke, readiness=self.ready)
        self.server = BrokerHTTPServer(("127.0.0.1", 0), self.app)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        if self.app.worker is not None:
            self.app.worker.join(timeout=3)
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.temporary.cleanup()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        defaults = {}
        if method == "POST":
            defaults = {"Content-Type": "application/json", "X-Broker-Request": "1"}
        if headers:
            defaults.update(headers)
        payload = json.dumps(body).encode() if isinstance(body, (dict, list)) else body
        connection.request(method, path, body=payload, headers=defaults)
        response = connection.getresponse()
        content = response.read()
        result = json.loads(content) if response.getheader("Content-Type", "").startswith("application/json") else content
        status, response_headers = response.status, dict(response.getheaders())
        connection.close()
        return status, result, response_headers

    def start(self):
        status, result, _ = self.request("POST", "/api/runs", {})
        self.assertEqual(status, 202, result)
        return result["job_id"]

    def finished(self, job_id):
        self.app.worker.join(timeout=3)
        self.assertFalse(self.app.worker.is_alive())
        status, job, _ = self.request("GET", "/api/runs/" + job_id)
        self.assertEqual(status, 200)
        return job

    def test_case_status_readiness_cache_and_static_security_headers(self):
        status, case, headers = self.request("GET", "/api/case")
        self.assertEqual(status, 200)
        self.assertTrue(case["case"]["fictional"])
        self.assertFalse(case["preflight"]["execution"]["model_called"])
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("script-src 'self'", headers["Content-Security-Policy"])
        for _ in range(2):
            status, state, _ = self.request("GET", "/api/status")
            self.assertEqual(status, 200)
            self.assertTrue(state["model_ready"])
            self.assertIsNone(state["latest_run"])
        self.ready.assert_called_once()
        self.assertEqual(self.request("GET", "/")[0], 200)
        self.assertEqual(self.request("GET", "/app.js")[0], 200)

    def test_post_boundaries_reject_cross_origin_headers_content_and_large_body(self):
        cases = [
            ({"X-Broker-Request": ""}, {}, 403),
            ({"Origin": "https://other.example"}, {}, 403),
            ({"Origin": "null"}, {}, 403),
            ({"Content-Type": "text/plain"}, {}, 415),
            ({}, b"x" * (MAX_BODY_BYTES + 1), 413),
            ({}, b"not json", 400),
            ({}, [], 400),
            ({}, {"unexpected": True}, 400),
        ]
        for headers, body, expected in cases:
            with self.subTest(headers=headers, expected=expected):
                status, _, returned = self.request("POST", "/api/runs", body, headers)
                self.assertEqual(status, expected)
                self.assertNotIn("Access-Control-Allow-Origin", returned)
        self.invoke.assert_not_called()

    def test_paths_cannot_reach_parent_dotfiles_symlinks_or_non_uuid_runs(self):
        for path in ("/../secret.txt", "/%2e%2e/secret.txt", "/%252e%252e/secret.txt", "/.env",
                     "/%2eenv", "/outside.txt", "/api/runs/not-a-uuid", "/api/runs/../../.env",
                     "/api/runs/" + str(uuid4()), "/broker/model.py"):
            with self.subTest(path=path):
                status, payload, _ = self.request("GET", path)
                self.assertEqual(status, 404)
                self.assertNotIn("outside source", json.dumps(payload))
                self.assertNotIn("MUST_NOT_LEAK", json.dumps(payload))

    def test_progress_precedes_each_invoke_single_worker_and_approval_after_completion(self):
        entered = {stage: threading.Event() for stage in STAGES}
        released = {stage: threading.Event() for stage in STAGES}

        def invoke(stage, prompt, schema):
            entered[stage].set()
            if not released[stage].wait(3):
                raise RuntimeError("test gate timed out")
            return fake_packet(stage, prompt, schema)

        self.app.invoke = invoke
        job_id = self.start()
        try:
            self.assertTrue(entered["watcher"].wait(2))
            self.assertEqual(self.request("POST", "/api/runs", {})[0], 409)
            approval_url = "/api/runs/" + job_id + "/approval"
            self.assertEqual(self.request("POST", approval_url, {"decision": "approve", "note": "early"})[0], 409)
            for index, stage in enumerate(STAGES):
                self.assertTrue(entered[stage].wait(2))
                status, job, _ = self.request("GET", "/api/runs/" + job_id)
                self.assertEqual(status, 200)
                self.assertEqual(job["current_stage"], stage)
                self.assertEqual([item["stage"] for item in job["stages"]], list(STAGES[:index]))
                saved = json.loads((self.root / "runs" / (job_id + ".json")).read_text())
                self.assertEqual(saved["current_stage"], stage)
                released[stage].set()
            job = self.finished(job_id)
            self.assertEqual(job["status"], "completed", job["error"])
            self.assertEqual(job["result"]["execution"]["model_calls"], 4)
            original_result = deepcopy(job["result"])
            for decision in ("approve", "reject"):
                status, job, _ = self.request("POST", approval_url, {"decision": decision, "note": "A review decision"})
                self.assertEqual(status, 200)
                self.assertEqual(set(job["approval"]), {"decision", "note", "decided_at"})
                self.assertEqual(job["approval"]["decision"], decision)
            self.assertEqual([entry["decision"] for entry in job["approval_history"]], ["approve", "reject"])
            self.assertEqual(job["result"], original_result)
            self.assertEqual(job["result"]["actions"], {"messages_sent": 0, "records_updated": 0})
            self.assertEqual(len(job["events"]), 12)
            self.assertEqual(self.request("GET", "/api/status")[1]["latest_run"], job)
            self.assertEqual(json.loads((self.root / "runs" / (job_id + ".json")).read_text()), job)
        finally:
            for gate in released.values():
                gate.set()

    def test_not_ready_does_not_start_model_and_same_origin_is_accepted(self):
        self.ready.return_value = False
        status, result, _ = self.request("POST", "/api/runs", {},
                                         {"Origin": f"http://127.0.0.1:{self.server.server_port}"})
        self.assertEqual(status, 503)
        self.invoke.assert_not_called()

    def test_model_exception_is_redacted_and_unlocks_next_run(self):
        self.invoke.side_effect = RuntimeError("API_TOKEN=private /Users/sensitive/file provider body")
        job_id = self.start()
        job = self.finished(job_id)
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["current_stage"], "watcher")
        for secret in ("API_TOKEN", "/Users", "provider body"):
            self.assertNotIn(secret, json.dumps(job))
        self.assertEqual(self.request("POST", "/api/runs/" + job_id + "/approval",
                                      {"decision": "approve", "note": "failed"})[0], 409)
        self.assertIsNone(self.request("GET", "/api/status")[1]["active_run_id"])
        self.invoke.side_effect = fake_packet
        self.assertEqual(self.finished(self.start())["status"], "completed")

    def test_blocked_stage_never_advances_or_accepts_approval(self):
        def blocked(stage, prompt, schema):
            packet = fake_packet(stage, prompt, schema)
            if stage == "matcher":
                packet.update(status="BLOCKED", reason_code="SOURCE_CONFLICT")
            return packet
        self.invoke.side_effect = blocked
        job_id = self.start()
        job = self.finished(job_id)
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["result"]["blocked_stage"], "matcher")
        self.assertEqual([call.args[0] for call in self.invoke.call_args_list], ["watcher", "matcher"])
        self.assertEqual(self.request("POST", "/api/runs/" + job_id + "/approval",
                                      {"decision": "reject", "note": "blocked"})[0], 409)

    def test_recovery_marks_interrupted_failed_without_replaying_model(self):
        job_id = self.start()
        job = self.finished(job_id)
        job.update(status="running", current_stage="matcher", result=None)
        path = self.root / "runs" / (job_id + ".json")
        path.write_text(json.dumps(job))
        invoke = Mock(side_effect=AssertionError("must not replay"))
        recovered = BrokerApplication(case_path=FIXTURE, web_root=self.web, runs_dir=self.root / "runs",
                                      invoke=invoke, readiness=lambda: True)
        restored = recovered.get_job(job_id)
        self.assertEqual(restored["status"], "failed")
        self.assertEqual(restored["events"][-1]["event"], "interrupted")
        self.assertEqual(json.loads(path.read_text())["status"], "failed")
        self.assertIsNone(recovered.active_run_id)
        invoke.assert_not_called()


class StartupTests(unittest.TestCase):
    @patch("broker.server.model_adapter.codex_binary", return_value="/new/codex")
    @patch("broker.server.subprocess.run")
    def test_readiness_uses_selected_binary_and_discards_provider_logs(self, run, binary):
        run.return_value.returncode = 0
        self.assertTrue(codex_logged_in())
        self.assertEqual(run.call_args.args[0], ["/new/codex", "login", "status"])
        self.assertEqual(run.call_args.kwargs["stdout"], subprocess.DEVNULL)
        self.assertEqual(run.call_args.kwargs["stderr"], subprocess.DEVNULL)
        run.return_value.returncode = 1
        self.assertFalse(codex_logged_in())

    def test_invalid_case_fails_before_server_or_model_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            path.write_text("{}")
            with self.assertRaises(ValueError):
                BrokerApplication(case_path=path, runs_dir=Path(directory) / "runs")
            self.assertFalse((Path(directory) / "runs").exists())


if __name__ == "__main__":
    unittest.main()
