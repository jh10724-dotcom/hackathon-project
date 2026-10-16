"""One isolated Codex invocation per role, using the runtime's own login store."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


class ModelError(RuntimeError):
    pass


def codex_binary() -> str | None:
    configured = os.environ.get("BROKER_CODEX_BIN")
    if configured:
        return configured
    installed = Path.home() / "broker-tools/node_modules/.bin/codex"
    return str(installed) if installed.is_file() else shutil.which("codex")


class CodexInvoker:
    def __init__(self, *, model: str | None = None, timeout: int = 180):
        self.model = model
        self.timeout = timeout

    def __call__(self, stage: str, prompt: str, schema: dict) -> dict:
        executable = codex_binary()
        if not executable:
            raise ModelError("Codex is not installed in this runtime. Install @openai/codex, then sign in with codex login --device-auth.")
        with tempfile.TemporaryDirectory(prefix=f"broker-{stage}-") as temp:
            output = Path(temp) / "packet.json"
            schema_path = Path(temp) / "schema.json"
            schema_path.write_text(json.dumps(schema))
            # Role calls only reason over supplied evidence, without action tools.
            command = [executable, "exec", "--ignore-user-config", "--skip-git-repo-check",
                       "--ephemeral", "--sandbox", "read-only", "--disable", "shell_tool",
                       "--disable", "unified_exec", "--disable", "apps", "--disable", "browser_use",
                       "--disable", "browser_use_external", "-c", 'web_search="disabled"',
                       "--color", "never", "--output-schema", str(schema_path),
                       "--output-last-message", str(output)]
            if self.model:
                command.extend(["--model", self.model])
            command.append("-")
            try:
                result = subprocess.run(command, input=prompt, text=True, capture_output=True, cwd=temp, timeout=self.timeout)
            except subprocess.TimeoutExpired as exc:
                raise ModelError(f"{stage} timed out; the pipeline did not advance") from exc
            except OSError as exc:
                raise ModelError("Could not start Codex in this runtime") from exc
            if result.returncode or not output.is_file():
                # Provider output may contain case material; don't echo it by default.
                raise ModelError(f"{stage} failed (Codex exit {result.returncode}). Check codex login status in this runtime.")
            try:
                packet = json.loads(output.read_text())
            except (ValueError, OSError) as exc:
                raise ModelError(f"{stage} did not return a readable JSON packet") from exc
            if not isinstance(packet, dict):
                raise ModelError(f"{stage} returned a non-object packet")
            return packet
