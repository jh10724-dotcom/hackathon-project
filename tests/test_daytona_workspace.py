"""Local transport tests: these never run the real Daytona CLI or call its API."""
import base64
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "daytona_workspace.py"
SPEC = importlib.util.spec_from_file_location("daytona_workspace", MODULE_PATH)
workspace = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = workspace
SPEC.loader.exec_module(workspace)


class SourceBoundaryTests(unittest.TestCase):
    def test_rejects_secrets_traversal_and_non_allowlisted_paths(self):
        rejected = (
            ".env", ".git/config", "data/auth.json", "data/aws-credentials.json",
            "scripts/secrets.py", "scripts/id_rsa", "scripts/client.pem",
            "broker/../data/case.json", "/broker/core.py", "./broker/core.py",
            "broker//core.py", "broker\\core.py", "broker/.codex/main.py",
            "broker/outputs/result.py", "outputs/review.json", "data/deep/case.json",
            "scripts/nested/run.py", "broker/core.py;echo bad", "scripts/key.key",
            "web/.env", "web/.env.js", "web/auth.js", "web/aws-credentials.js",
            "web/secrets.css", "web/token.html", "web/nested/app.js", "web/config.json",
            "web/../data/case.json", "web/app.js.map",
        )
        for name in rejected:
            with self.subTest(name=name), self.assertRaises(workspace.WorkspaceError):
                workspace.validate_source_path(name)

    def test_collects_only_source_allowlist(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            allowed = ("broker/core.py", "broker/nested/tool.py", "data/case.json",
                       "schemas/result.json", "prompts/review.md", "scripts/run.sh",
                       "README.md", "pyproject.toml", "web/index.html", "web/app.js", "web/style.css")
            ignored = (".env", ".git/config", ".codex/auth.json", "outputs/result.json",
                       "data/document.txt", "scripts/nested/no.py", "tests/test_core.py",
                       "web/.env", "web/config.json", "web/app.js.map", "web/nested/app.js")
            for name in allowed + ignored:
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("fixture")
            self.assertEqual(set(workspace.collect_source_files(root)), set(allowed))

    def test_web_asset_globs_do_not_bypass_credential_name_checks(self):
        for name in ("auth.js", "aws-credentials.js", "secrets.css", "token.html", ".env.js"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "web").mkdir()
                (root / "web/index.html").write_text("<main>Broker review</main>")
                (root / "web" / name).write_text("must not upload")
                with self.assertRaises(workspace.WorkspaceError):
                    workspace.collect_source_files(root)

    def test_rejects_symlink_file_even_when_target_is_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "broker").mkdir()
            (root / "secret.txt").write_text("must not upload")
            (root / "broker/core.py").symlink_to(root / "secret.txt")
            with self.assertRaisesRegex(workspace.WorkspaceError, "Symlink"):
                workspace.collect_source_files(root)

    def test_rejects_allowlisted_file_inside_symlink_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "other").mkdir()
            (root / "other/case.json").write_text("{}")
            (root / "data").symlink_to(root / "other", target_is_directory=True)
            with self.assertRaisesRegex(workspace.WorkspaceError, "Symlink"):
                workspace.collect_source_files(root)

    def test_caps_file_and_combined_size_before_transfer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data").mkdir()
            (root / "data/a.json").write_bytes(b"12345")
            with patch.object(workspace, "MAX_FILE_BYTES", 4), self.assertRaisesRegex(workspace.WorkspaceError, "File exceeds"):
                workspace.collect_source_files(root)
            (root / "data/b.json").write_bytes(b"12345")
            with patch.object(workspace, "MAX_TRANSFER_BYTES", 9), self.assertRaisesRegex(workspace.WorkspaceError, "Transfer exceeds"):
                workspace.collect_source_files(root)


class ReceiverTests(unittest.TestCase):
    def payload(self, root, path="data/case.json", data=b'{"value": "$(touch bad)"}'):
        return {"root": str(root), "path": path, "data": base64.b64encode(data).decode(),
                "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}

    def test_generated_receiver_runs_and_preserves_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            # Resolve macOS /var -> /private/var before checking ancestor symlinks.
            root = Path(directory).resolve() / "workspace"
            payload = self.payload(root)
            encoded = base64.b64encode(json.dumps(payload).encode()).decode()
            result = subprocess.run([sys.executable, "-c", workspace.receiver_script(), encoded],
                                    capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((root / "data/case.json").read_bytes(), base64.b64decode(payload["data"]))
            receipt = json.loads(result.stdout.removeprefix(workspace.RECEIPT_PREFIX))
            self.assertEqual(receipt["sha256"], payload["sha256"])
            self.assertFalse((root / "bad").exists())

    def test_remote_rejects_traversal_and_wrong_digest_before_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "workspace"
            bad = self.payload(root, "../outside.json")
            with self.assertRaises(workspace.WorkspaceError):
                workspace.receive_file(bad)
            bad = self.payload(root)
            bad["sha256"] = "wrong"
            with self.assertRaisesRegex(workspace.WorkspaceError, "digest"):
                workspace.receive_file(bad)
            self.assertFalse(root.exists())

    def test_receiver_accepts_static_web_bytes_but_refuses_credential_names(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "workspace"
            content = b'window.brokerTitle = "Broker review";'
            receipt = workspace.receive_file(self.payload(root, "web/app.js", content))
            self.assertEqual((root / "web/app.js").read_bytes(), content)
            self.assertEqual(receipt["path"], "web/app.js")
            with self.assertRaises(workspace.WorkspaceError):
                workspace.receive_file(self.payload(root, "web/credentials.js", b"must not upload"))
            self.assertFalse((root / "web/credentials.js").exists())

    def test_remote_refuses_symlink_destination_without_modifying_target(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            root = base / "workspace"
            (root / "data").mkdir(parents=True)
            outside = base / "original.json"
            outside.write_text("original")
            (root / "data/case.json").symlink_to(outside)
            with self.assertRaisesRegex(workspace.WorkspaceError, "Symlink"):
                workspace.receive_file(self.payload(root))
            self.assertEqual(outside.read_text(), "original")


class TransportTests(unittest.TestCase):
    def helper(self, **kwargs):
        return workspace.DaytonaWorkspace(cli="/fake/daytona", **kwargs)

    @patch.object(workspace.subprocess, "run")
    def test_remote_failure_exit_code_reaches_caller(self, run):
        run.return_value = subprocess.CompletedProcess([], 7, "remote rejected request", "")
        with self.assertRaises(workspace.WorkspaceError) as failure:
            self.helper().run("preflight")
        self.assertEqual(failure.exception.exit_code, 7)
        self.assertIn("remote rejected request", str(failure.exception))
        self.assertIs(run.call_args.kwargs["shell"], False)

    @patch.object(workspace.subprocess, "run")
    def test_arguments_keep_boundaries_without_shell_expansion(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, "ok\n", "")
        arguments = ["--case", "a file's $(touch bad).json"]
        self.helper().run("agent", arguments)
        command = run.call_args.args[0]
        self.assertEqual(shlex.split(command[-1]), ["python3", "-m", "broker.cli", "review", *arguments])
        self.assertEqual(command[-2], "--")
        self.assertIs(run.call_args.kwargs["shell"], False)

    @patch.object(workspace.subprocess, "run")
    def test_create_is_private_autostopping_and_uses_snapshot_resources(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, "created", "")
        self.helper().create()
        command = run.call_args.args[0]
        self.assertIn("--public=false", command)
        self.assertEqual(command[command.index("--auto-stop") + 1], "10")
        self.assertNotIn("--cpu", command)
        self.assertNotIn("--memory", command)

    def test_missing_receipt_fails_sync_even_when_cli_returns_zero(self):
        helper = self.helper()
        with patch.object(workspace, "collect_source_files", return_value={"data/case.json": b"{}"}), \
             patch.object(helper, "_exec", return_value="nothing transferred") as execute:
            with self.assertRaisesRegex(workspace.WorkspaceError, "acknowledgement"):
                helper.sync()
        execute.assert_called_once()

    def test_sync_transfers_sources_with_verified_receipts_using_local_receiver(self):
        real_run = subprocess.run
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            source = base / "source"
            remote = base / "remote"
            (source / "broker").mkdir(parents=True)
            (source / "broker/core.py").write_text("print('literal $(touch bad)')\n")
            (source / ".env").write_text("NEVER_TRANSFER_THIS=private")
            helper = self.helper(project_root=source, remote_dir=str(remote))

            def local_receiver(command, **kwargs):
                self.assertEqual(command[0], "/fake/daytona")
                arguments = shlex.split(command[-1])
                self.assertEqual(arguments[:2], ["python3", "-c"])
                return real_run([sys.executable, *arguments[1:]], **kwargs)

            with patch.object(workspace.subprocess, "run", side_effect=local_receiver):
                receipt = helper.sync()
            self.assertEqual(receipt["files"], ["broker/core.py"])
            self.assertEqual((remote / "broker/core.py").read_bytes(), (source / "broker/core.py").read_bytes())
            self.assertFalse((remote / ".env").exists())
            self.assertFalse((remote / "bad").exists())

    def test_sync_stops_after_remote_error_and_preserves_exit_code(self):
        helper = self.helper()
        with patch.object(workspace, "collect_source_files", return_value={"data/a.json": b"{}", "data/b.json": b"{}"}), \
             patch.object(helper, "_exec", side_effect=workspace.WorkspaceError("remote failure", 23)) as execute:
            with self.assertRaises(workspace.WorkspaceError) as failure:
                helper.sync()
        self.assertEqual(failure.exception.exit_code, 23)
        execute.assert_called_once()

    @patch.object(workspace.subprocess, "run", side_effect=subprocess.TimeoutExpired("redacted", 10))
    def test_timeout_is_reported_as_unknown_remote_completion(self, run):
        with self.assertRaises(workspace.WorkspaceError) as failure:
            self.helper().status()
        self.assertEqual(failure.exception.exit_code, 124)
        self.assertIn("completion is unknown", str(failure.exception))

    @patch.object(workspace, "DaytonaWorkspace")
    def test_cli_forwards_remainder_and_returns_remote_exit_code(self, cls):
        cls.return_value.run.side_effect = workspace.WorkspaceError("remote failed", 9)
        with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            code = workspace.main(["run", "--name", "existing-demo", "agent", "--", "--case", "data/ap001.json"])
        self.assertEqual(code, 9)
        cls.return_value.run.assert_called_once_with("agent", ["--case", "data/ap001.json"])
        self.assertEqual(cls.call_args.kwargs["name"], "existing-demo")

    def test_invalid_sandbox_identifier_is_rejected(self):
        for name in ("--all", "../../other", "demo;echo bad"):
            with self.subTest(name=name), self.assertRaises(workspace.WorkspaceError):
                self.helper(name=name)

    def test_cli_path_uses_explicit_env_then_project_portable_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "outputs/broker-agent"
            binary = root / "work/bin/daytona"
            binary.parent.mkdir(parents=True)
            binary.write_text("unused")
            binary.chmod(0o700)
            with patch.dict(os.environ, {"DAYTONA_CLI": "/chosen/daytona"}), \
                 patch.object(workspace.shutil, "which", return_value="/chosen/daytona"):
                self.assertEqual(workspace.find_daytona_cli(project), "/chosen/daytona")
            with patch.dict(os.environ, {}, clear=True), patch.object(workspace.shutil, "which", return_value=None):
                self.assertEqual(workspace.find_daytona_cli(project), str(binary.resolve()))


if __name__ == "__main__":
    unittest.main()
