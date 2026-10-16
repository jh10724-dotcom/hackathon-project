from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from broker.pipeline import run_pipeline
from broker.server import BrokerApplication
from test_server import fake_packet


class ImportTests(unittest.TestCase):
    def test_recorded_run_import_validates_and_never_replays_or_overwrites_review(self):
        with tempfile.TemporaryDirectory() as root:
            model = Mock(side_effect=AssertionError("Import must not invoke a model"))
            app = BrokerApplication(runs_dir=Path(root), invoke=model, readiness=lambda: True)
            result = run_pipeline(app.case, app.preflight, fake_packet)
            job = app.import_completed_run(result)
            self.assertEqual(job["result"], result)
            self.assertEqual(job["created_at"], result["created_at"])
            self.assertIsNone(job["approval"])
            app.approve(job["id"], "reject", "Test review")
            self.assertEqual(app.import_completed_run(result)["approval"]["decision"], "reject")
            restored = BrokerApplication(runs_dir=Path(root), invoke=model, readiness=lambda: True)
            self.assertEqual(restored.get_job(job["id"])["result"], result)
            for mutation in ("digest", "quote", "missing_stage", "blocked", "actions"):
                bad = deepcopy(result)
                if mutation == "digest": bad["source_revision"] = "wrong"
                elif mutation == "quote": bad["stages"][0]["source_refs"][0]["excerpt"] = "Invented evidence"
                elif mutation == "missing_stage": bad["stages"].pop()
                elif mutation == "blocked": bad["stages"][0]["status"] = "BLOCKED"
                else: bad["actions"]["messages_sent"] = 1
                with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                    app.import_completed_run(bad)
            model.assert_not_called()
