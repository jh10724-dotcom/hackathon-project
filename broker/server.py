"""Small local review UI server; approving a packet records only an audit decision.

Run with ``python3 -m broker.server``. For a private Daytona preview, explicitly
bind with ``--host 0.0.0.0``. The default bind is loopback on port 8765.
"""
from __future__ import annotations

import argparse
import hashlib
from copy import deepcopy
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import time
from typing import Callable
from urllib.parse import unquote, urlsplit
from uuid import UUID, uuid4

from broker.core import analyze_case
from broker import model as model_adapter
from broker.pipeline import PipelineError, STAGES, run_pipeline, _validate_packet
from broker.updates import UpdateStore


ROOT = Path(__file__).resolve().parents[1]
MAX_BODY_BYTES = 16 * 1024
MAX_STATIC_BYTES = 8 * 1024 * 1024
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; "
    "img-src 'self' data:; font-src 'self'; connect-src 'self'; "
    "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
)


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def valid_job_id(value: str) -> bool:
    try:
        parsed = UUID(value)
        return parsed.version == 4 and str(parsed) == value
    except (ValueError, TypeError, AttributeError):
        return False


def codex_logged_in() -> bool:
    """Check only login status; provider output never enters logs or responses."""
    executable = model_adapter.codex_binary()
    if not executable:
        return False
    try:
        result = subprocess.run([executable, "login", "status"], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                timeout=5, check=False)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


class BrokerApplication:
    """Shared state and one worker. Tests inject invoke/readiness, never real AI."""
    def __init__(self, *, case_path: Path = ROOT / "data/ap001.json",
                 web_root: Path = ROOT / "web", runs_dir: Path = ROOT / "artifacts/runs",
                 invoke: Callable | None = None, readiness: Callable[[], bool] | None = None,
                 public_origin: str | None = None):
        self.public_origin = public_origin if public_origin is not None else os.environ.get("BROKER_PUBLIC_ORIGIN", "")
        if self.public_origin:
            origin = urlsplit(self.public_origin)
            if (origin.scheme not in {"http", "https"} or not origin.netloc or origin.path
                    or origin.query or origin.fragment or origin.username is not None
                    or origin.password is not None):
                raise ValueError("BROKER_PUBLIC_ORIGIN must be an exact origin without a path")
        self.case_path = Path(case_path)
        initial_bytes = self.case_path.read_bytes()
        self.case = json.loads(initial_bytes)
        self.preflight = analyze_case(self.case)
        self._observed_case_hash = hashlib.sha256(initial_bytes).hexdigest()
        self.web_root = Path(web_root).resolve()
        self.runs_dir = Path(runs_dir)
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self.invoke = invoke if invoke is not None else model_adapter.CodexInvoker(timeout=180)
        self.readiness = readiness if readiness is not None else codex_logged_in
        self.lock = threading.RLock()
        self.jobs: dict[str, dict] = {}
        self.active_run_id: str | None = None
        self.worker: threading.Thread | None = None
        self._ready_until = 0.0
        self._ready_value = False
        self._recover()
        self.updates = UpdateStore(self.runs_dir.parent / "updates.json")
        self._monitor_stop = threading.Event()
        self._monitor_thread = None

    def updates_response(self) -> dict:
        with self.lock:
            return self.updates.feed(self.jobs.values())

    def update_action(self, item_id: str, action: str) -> dict:
        if not isinstance(action, str) or action not in {"read", "unread", "snooze"}:
            raise ApiError(400, "Use read, unread, or snooze")
        with self.lock:
            self.updates.feed(self.jobs.values())
            try:
                self.updates.act(item_id, action)
            except KeyError:
                raise ApiError(404, "Update not found") from None
            return self.updates.feed(self.jobs.values())

    def set_monitor(self, enabled: bool) -> dict:
        if type(enabled) is not bool:
            raise ApiError(400, "Enabled must be a boolean")
        with self.lock:
            self.updates.monitor["enabled"] = enabled
            self.updates.save()
            return self.updates.feed(self.jobs.values())

    def check_updates(self) -> dict:
        """Inspect the selected case file only. Never invokes any model."""
        with self.lock:
            monitor = self.updates.monitor
            monitor["last_checked_at"] = utc_now()
            try:
                raw = self.case_path.read_bytes()
                digest = hashlib.sha256(raw).hexdigest()
                if digest != self._observed_case_hash:
                    candidate = json.loads(raw)
                    preflight = analyze_case(candidate)
                    previous_revision = self.preflight["source_revision"]
                    old = {source["source_id"]: source for source in self.case["sources"]}
                    new = {source["source_id"]: source for source in candidate["sources"]}
                    added = sorted(new.keys() - old.keys())
                    removed = sorted(old.keys() - new.keys())
                    changed = sorted(key for key in new.keys() & old.keys() if new[key] != old[key])
                    self.case, self.preflight = candidate, preflight
                    self._observed_case_hash = digest
                    monitor["error"] = None
                    if previous_revision != preflight["source_revision"]:
                        now = utc_now()
                        monitor["last_changed_at"] = now
                        change_id = hashlib.sha256((previous_revision + preflight["source_revision"]).encode()).hexdigest()[:32]
                        self.updates.upsert({"id": "source-" + change_id, "kind": "source_changed",
                            "title": "Case sources changed", "summary":
                            "New case evidence was detected. Review the current sources before the next decision.",
                            "created_at": now, "run_id": None, "stage": None,
                            "source_ids": sorted(added + changed + removed), "status": "unread",
                            "snoozed_until": None, "action_label": "Review case sources"})
                else:
                    monitor["error"] = None
            except (OSError, ValueError, TypeError, KeyError):
                monitor["error"] = "Case file could not be validated. The last valid evidence is still displayed."
            self.updates.save()
            return self.updates.feed(self.jobs.values())

    def start_monitor(self) -> None:
        """Called only by main(); constructing an application does not start it."""
        def monitor_loop():
            while not self._monitor_stop.wait(30):
                try:
                    with self.lock:
                        enabled = self.updates.monitor["enabled"]
                    if enabled:
                        self.check_updates()
                except Exception:
                    with self.lock:
                        self.updates.monitor["error"] = "The file monitor could not save its latest check."
        if self._monitor_thread is None:
            self._monitor_thread = threading.Thread(target=monitor_loop, daemon=True, name="broker-file-monitor")
            self._monitor_thread.start()

    def stop_monitor(self) -> None:
        self._monitor_stop.set()
        if self._monitor_thread is not None:
            self._monitor_thread.join(timeout=5)

    def _write_atomic(self, job: dict) -> None:
        if not valid_job_id(job["id"]):
            raise ValueError("Invalid run identity")
        destination = self.runs_dir / (job["id"] + ".json")
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.runs_dir,
                                             prefix=".run-", suffix=".tmp", delete=False) as handle:
                temporary = Path(handle.name)
                json.dump(job, handle, ensure_ascii=False, allow_nan=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()

    def _commit_locked(self, job: dict, event: str, **event_fields) -> None:
        job["updated_at"] = utc_now()
        job.setdefault("events", []).append({"event": event, "at": job["updated_at"], **event_fields})
        self._write_atomic(job)
        self.jobs[job["id"]] = job

    def _recover(self) -> None:
        with self.lock:
            for path in sorted(self.runs_dir.glob("*.json")):
                if path.is_symlink() or not valid_job_id(path.stem):
                    continue
                try:
                    job = json.loads(path.read_text(encoding="utf-8"))
                    if not isinstance(job, dict) or job.get("id") != path.stem:
                        continue
                    if job.get("status") not in {"running", "completed", "failed"}:
                        continue
                    if not isinstance(job.get("stages"), list) or not isinstance(job.get("events", []), list):
                        continue
                    if not isinstance(job.get("created_at"), str):
                        continue
                    job.setdefault("approval", None)
                    job.setdefault("approval_history", [])
                    job.setdefault("result", None)
                    job.setdefault("current_stage", None)
                    job.setdefault("error", None)
                    if job["status"] == "running":
                        job.update(status="failed", error="This run was interrupted by a server restart. Start a new run to try again.")
                        self._commit_locked(job, "interrupted")
                    else:
                        self.jobs[job["id"]] = job
                except (OSError, ValueError, TypeError, KeyError):
                    # A damaged audit file must never trigger another paid run.
                    continue

    def import_completed_run(self, result: dict) -> dict:
        """Operator-only import of a recorded CLI run; never invokes a model.

        There is no HTTP route for imports. Validation proves packet integrity,
        not execution provenance; operators must supply their actual run receipt.
        """
        result = deepcopy(result)
        if (not isinstance(result, dict)
                or result.get("schema_version") != "broker-agent-run/v1"
                or not valid_job_id(result.get("run_id"))
                or result.get("case_id") != self.case["case_id"]
                or result.get("source_revision") != self.preflight["source_revision"]
                or result.get("preflight") != self.preflight
                or result.get("status") != "NEEDS_BROKER_REVIEW"
                or result.get("blocked_stage") is not None
                or result.get("completed_stages") != list(STAGES)
                or result.get("actions") != {"messages_sent": 0, "records_updated": 0}
                or result.get("execution") != {"kind": "four_agent_pipeline", "model_calls": 4, "completed_stages": list(STAGES)}
                or not isinstance(result.get("stages"), list) or len(result["stages"]) != 4):
            raise ValueError("Import must be a complete review of the selected evidence")
        created, finished = (datetime.fromisoformat(result[key]) for key in ("created_at", "finished_at"))
        if created.tzinfo is None or finished.tzinfo is None or finished < created:
            raise ValueError("Import timestamps are invalid")
        previous = []
        for stage, packet in zip(STAGES, result["stages"]):
            _validate_packet(packet, stage, result, self.case, self.preflight, previous)
            if packet["status"] == "BLOCKED":
                raise ValueError("Cannot import a blocked run as completed")
            previous.append(packet)
        with self.lock:
            job_id = result["run_id"]
            if job_id in self.jobs:
                if self.jobs[job_id].get("result") != result:
                    raise ValueError("An existing run has different contents")
                return deepcopy(self.jobs[job_id])
            job = {"id": job_id, "status": "completed", "current_stage": None,
                   "stages": [{"stage": p["stage"], "status": p["status"], "packet": p} for p in previous],
                   "result": result, "error": None, "approval": None, "approval_history": [],
                   "created_at": result["created_at"], "source_revision": result["source_revision"], "events": []}
            self._commit_locked(job, "imported_completed_run", original_finished_at=result["finished_at"])
            return deepcopy(job)

    def _model_ready_locked(self) -> bool:
        now = time.monotonic()
        if now >= self._ready_until:
            try:
                self._ready_value = bool(self.readiness())
            except Exception:
                self._ready_value = False
            self._ready_until = time.monotonic() + 10
        return self._ready_value

    def case_response(self) -> dict:
        with self.lock:
            return {"case": deepcopy(self.case), "preflight": deepcopy(self.preflight)}

    def status_response(self) -> dict:
        with self.lock:
            latest = max(self.jobs.values(), key=lambda job: job["created_at"], default=None)
            return {"runtime": "daytona" if os.environ.get("BROKER_RUNTIME") == "daytona" else "local",
                    "model_ready": self._model_ready_locked(), "active_run_id": self.active_run_id,
                    "latest_run": deepcopy(latest)}

    def get_job(self, job_id: str) -> dict:
        with self.lock:
            if not valid_job_id(job_id) or job_id not in self.jobs:
                raise ApiError(404, "Run not found")
            return deepcopy(self.jobs[job_id])

    def start_run(self) -> str:
        self.check_updates()
        with self.lock:
            if self.active_run_id is not None:
                raise ApiError(409, "A review is already running")
            if not self._model_ready_locked():
                raise ApiError(503, "Codex is not signed in in this runtime")
            if self.updates.monitor["error"]:
                raise ApiError(409, "Validate the changed case file before starting a review")
            job_id = str(uuid4())
            case, preflight = deepcopy(self.case), deepcopy(self.preflight)
            job = {"id": job_id, "case_id": case["case_id"], "status": "running", "current_stage": None, "stages": [],
                   "result": None, "error": None, "approval": None, "approval_history": [],
                   "created_at": utc_now(), "source_revision": self.preflight["source_revision"], "events": []}
            self._commit_locked(job, "started")
            self.active_run_id = job_id
            worker = threading.Thread(target=self._run_worker, args=(job_id, case, preflight), daemon=True,
                                      name="broker-review")
            self.worker = worker
            try:
                worker.start()
            except Exception:
                self._fail_locked(job_id, "The review worker could not start")
                raise ApiError(503, "The review worker could not start") from None
            return job_id

    def _fail_locked(self, job_id: str, message: str) -> None:
        job = deepcopy(self.jobs[job_id])
        job.update(status="failed", error=message)
        try:
            self._commit_locked(job, "failed", stage=job["current_stage"])
        except Exception:
            # Keep the API fail-closed even if the audit volume becomes unavailable.
            job["error"] = "The run stopped because its audit record could not be saved"
            self.jobs[job_id] = job
        self.active_run_id = None

    def _run_worker(self, job_id: str, case: dict, preflight: dict) -> None:
        def invoke(stage: str, prompt: str, schema: dict) -> dict:
            with self.lock:
                job = deepcopy(self.jobs[job_id])
                job["current_stage"] = stage
                self._commit_locked(job, "stage_started", stage=stage)
            return self.invoke(stage, prompt, schema)

        def on_stage(stage: str, packet: dict) -> None:
            with self.lock:
                job = deepcopy(self.jobs[job_id])
                job["stages"].append({"stage": stage, "status": packet["status"], "packet": deepcopy(packet)})
                self._commit_locked(job, "stage_completed", stage=stage, status=packet["status"])

        try:
            result = run_pipeline(case, preflight, invoke, on_stage=on_stage)
            with self.lock:
                job = deepcopy(self.jobs[job_id])
                job["result"] = result
                if result["status"] == "BLOCKED":
                    job.update(status="failed", current_stage=result["blocked_stage"],
                               error="An agent blocked this review. Review its evidence before starting again.")
                else:
                    job.update(status="completed", current_stage=None, error=None)
                self._commit_locked(job, job["status"])
                self.active_run_id = None
        except Exception as exc:
            with self.lock:
                stage = self.jobs[job_id].get("current_stage")
                label = stage.title() if stage in STAGES else "The review"
                message = f"{label} could not complete. No further agents were run."
                if isinstance(exc, PipelineError) and exc.code != "TOOL_ERROR":
                    message = f"{label}'s handoff did not pass validation. No further agents were run."
                self._fail_locked(job_id, message)

    def approve(self, job_id: str, decision: str, note: str) -> dict:
        if not isinstance(decision, str) or decision not in {"approve", "reject"} or not isinstance(note, str) or len(note) > 4000:
            raise ApiError(400, "Use approve or reject with a note of at most 4000 characters")
        self.check_updates()
        with self.lock:
            job = self.get_job(job_id)
            result = job.get("result")
            if self.updates.monitor["error"]:
                raise ApiError(409, "Validate the changed case file before recording a decision")
            if job.get("source_revision") != self.preflight["source_revision"]:
                raise ApiError(409, "The case sources changed. Run a new review before recording a decision.")
            if (job["status"] != "completed" or not isinstance(result, dict)
                    or result.get("status") != "NEEDS_BROKER_REVIEW"
                    or result.get("completed_stages") != list(STAGES)):
                raise ApiError(409, "Only a completed four-agent review can receive a decision")
            approval = {"decision": decision, "note": note, "decided_at": utc_now()}
            job["approval"] = approval
            job["approval_history"].append(deepcopy(approval))
            self._commit_locked(job, "approval_recorded", decision=decision)
            return deepcopy(job)


def request_path(raw: str) -> str:
    try:
        parsed = urlsplit(raw)
        path = unquote(parsed.path, encoding="utf-8", errors="strict")
    except (ValueError, UnicodeError):
        raise ApiError(400, "Invalid request path") from None
    if parsed.scheme or parsed.netloc or not path.startswith("/"):
        raise ApiError(400, "Invalid request path")
    parts = path.split("/")
    if (any(part.startswith(".") for part in parts if part) or "\\" in path or "%" in path
            or any(ord(char) < 32 or ord(char) == 127 for char in path)):
        raise ApiError(404, "Not found")
    return path


class BrokerHandler(BaseHTTPRequestHandler):
    server_version = "Broker"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    @property
    def app(self) -> BrokerApplication:
        return self.server.app

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, format, *args) -> None:
        # URLs and provider/case material do not belong in an access log.
        pass

    def _respond(self, status: int, data: bytes, content_type: str) -> None:
        self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, status: int, payload: dict) -> None:
        self._respond(status, json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8"),
                      "application/json; charset=utf-8")

    def send_error(self, code, message=None, explain=None) -> None:
        self._json(code, {"error": "Request was not accepted"})

    def _post_body(self) -> dict:
        if self.headers.get_all("X-Broker-Request", []) != ["1"]:
            raise ApiError(403, "A broker request header is required")
        origins = self.headers.get_all("Origin", [])
        hosts = self.headers.get_all("Host", [])
        if len(hosts) != 1 or len(origins) > 1:
            raise ApiError(403, "Request origin is not permitted")
        if origins:
            try:
                origin = urlsplit(origins[0])
                matches = (origin.scheme in {"http", "https"}
                           and (origin.netloc.lower() == hosts[0].lower()
                                or origins[0].lower() == self.app.public_origin.lower())
                           and not origin.path and not origin.query and not origin.fragment
                           and origin.username is None and origin.password is None)
            except ValueError:
                matches = False
            if not matches:
                raise ApiError(403, "Request origin is not permitted")
        content_types = self.headers.get_all("Content-Type", [])
        if len(content_types) != 1 or content_types[0].split(";", 1)[0].strip().lower() != "application/json":
            raise ApiError(415, "Use application/json")
        lengths = self.headers.get_all("Content-Length", [])
        if self.headers.get("Transfer-Encoding") is not None or len(lengths) != 1 or not re.fullmatch(r"[0-9]+", lengths[0]):
            raise ApiError(400, "A single content length is required")
        if len(lengths[0]) > 10 or int(lengths[0]) > MAX_BODY_BYTES:
            raise ApiError(413, "Request body is too large")
        raw = self.rfile.read(int(lengths[0]))
        if len(raw) != int(lengths[0]):
            raise ApiError(400, "Incomplete request body")
        try:
            body = json.loads(raw.decode("utf-8"), parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
        except (ValueError, UnicodeError, RecursionError):
            raise ApiError(400, "Use a valid JSON object") from None
        if not isinstance(body, dict):
            raise ApiError(400, "Use a JSON object")
        return body

    def _get(self) -> None:
        path = request_path(self.path)
        if path == "/api/case":
            self._json(200, self.app.case_response())
        elif path == "/api/status":
            self._json(200, self.app.status_response())
        elif path == "/api/updates":
            self._json(200, self.app.updates_response())
        elif path.startswith("/api/runs/"):
            self._json(200, self.app.get_job(path[len("/api/runs/"):]))
        elif path.startswith("/api/"):
            raise ApiError(404, "Not found")
        else:
            relative = "index.html" if path == "/" else path.lstrip("/")
            target = (self.app.web_root / relative).resolve()
            try:
                resolved_relative = target.relative_to(self.app.web_root)
            except ValueError:
                raise ApiError(404, "Not found") from None
            if any(part.startswith(".") for part in resolved_relative.parts):
                raise ApiError(404, "Not found")
            if not target.is_file() or target.stat().st_size > MAX_STATIC_BYTES:
                raise ApiError(404, "Not found")
            content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            self._respond(200, target.read_bytes(), content_type)

    def _post(self) -> None:
        path = request_path(self.path)
        body = self._post_body()
        if path == "/api/runs":
            if body:
                raise ApiError(400, "Run requests must be an empty JSON object")
            self._json(202, {"job_id": self.app.start_run()})
        elif path == "/api/updates/check":
            if body:
                raise ApiError(400, "Check requests must be an empty JSON object")
            self._json(200, self.app.check_updates())
        elif path == "/api/monitor":
            if set(body) != {"enabled"}:
                raise ApiError(400, "An enabled boolean is required")
            self._json(200, self.app.set_monitor(body["enabled"]))
        elif path.startswith("/api/updates/"):
            item_id = path[len("/api/updates/"):]
            if not re.fullmatch(r"[a-z0-9_-]{1,100}", item_id):
                raise ApiError(404, "Update not found")
            if set(body) != {"action"}:
                raise ApiError(400, "An update action is required")
            self._json(200, self.app.update_action(item_id, body["action"]))
        else:
            match = re.fullmatch(r"/api/runs/([^/]+)/approval", path)
            if not match or not valid_job_id(match[1]):
                raise ApiError(404, "Not found")
            if set(body) != {"decision", "note"}:
                raise ApiError(400, "A decision and note are required")
            self._json(200, self.app.approve(match[1], body["decision"], body["note"]))

    def _dispatch(self, method: Callable[[], None]) -> None:
        try:
            method()
        except ApiError as exc:
            self._json(exc.status, {"error": str(exc)})
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception:
            self._json(500, {"error": "The request could not be completed"})

    def do_GET(self) -> None:
        self._dispatch(self._get)

    def do_POST(self) -> None:
        self._dispatch(self._post)

    def do_OPTIONS(self) -> None:
        self._json(405, {"error": "Method not allowed"})


class BrokerHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], app: BrokerApplication):
        self.app = app
        super().__init__(address, BrokerHandler)

    def handle_error(self, request, client_address) -> None:
        # Base server tracebacks can contain local paths or case data.
        pass


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--case", type=Path, default=ROOT / "data/ap001.json")
    args = parser.parse_args(argv)
    try:
        app = BrokerApplication(case_path=args.case)
        server = BrokerHTTPServer((args.host, args.port), app)
    except Exception:
        print("Could not start the broker server. Check the case file, audit directory, and listening address.")
        return 1
    print(f"Broker review is available at http://{args.host}:{server.server_port}", flush=True)
    app.start_monitor()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.stop_monitor()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
