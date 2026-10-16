#!/usr/bin/env python3
"""Manage the demo workspace through an already authenticated Daytona CLI.

Examples (from the project root):
    python3 scripts/daytona_workspace.py status
    python3 scripts/daytona_workspace.py create
    python3 scripts/daytona_workspace.py sync
    python3 scripts/daytona_workspace.py run preflight
    python3 scripts/daytona_workspace.py run agent -- --case data/ap001.json

Only create creates a sandbox. Sync and run require an existing, running one.
No credentials are read or copied. Login separately with `daytona login`.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import inspect
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Sequence


DEFAULT_NAME = "broker-agent-demo"
DEFAULT_REMOTE_DIR = "/home/daytona/broker-agent"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
MAX_FILE_BYTES = 48 * 1024
MAX_TRANSFER_BYTES = 1024 * 1024
MAX_FILES = 256
SOURCE_GLOBS = (
    "broker/**/*.py", "data/*.json", "schemas/*.json", "prompts/*.md",
    "web/*.html", "web/*.js", "web/*.css",
    "scripts/*", "README", "README.md", "pyproject.toml",
)
RECEIPT_PREFIX = "BROKER_SYNC_OK="


class WorkspaceError(RuntimeError):
    def __init__(self, message: str, exit_code: int = 1):
        super().__init__(message)
        self.exit_code = exit_code


def _forbidden_component(part: str) -> bool:
    lowered = part.lower()
    if lowered.startswith(".") or lowered in {
        "outputs", "output", "work", "node_modules", "__pycache__", "venv",
    }:
        return True
    stem = lowered.rsplit(".", 1)[0]
    words = set(re.split(r"[-_.]", stem))
    return bool(words & {
        "auth", "credential", "credentials", "secret", "secrets", "token",
        "tokens", "keys", "password", "passwords",
    }) or lowered.endswith((".pem", ".key", ".p12", ".pfx")) or lowered in {
        "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa",
    }


def validate_source_path(relative: str) -> str:
    """Reject unsafe names even if a caller bypasses the file glob allowlist."""
    if not isinstance(relative, str) or not re.fullmatch(r"[A-Za-z0-9_./-]+", relative):
        raise WorkspaceError("Source path contains unsupported characters")
    path = PurePosixPath(relative)
    if path.is_absolute() or path.as_posix() != relative or any(
        part in {".", ".."} or _forbidden_component(part) for part in path.parts
    ):
        raise WorkspaceError(f"Source path is excluded: {relative}")
    parts = path.parts
    allowed = relative in {"README", "README.md", "pyproject.toml"}
    if len(parts) >= 2 and parts[0] == "broker" and path.suffix == ".py":
        allowed = True
    if len(parts) == 2:
        allowed = allowed or (
            parts[0] in {"data", "schemas"} and path.suffix == ".json"
        ) or (parts[0] == "prompts" and path.suffix == ".md") or (
            parts[0] == "web" and path.suffix in {".html", ".js", ".css"}
        ) or parts[0] == "scripts"
    if not allowed:
        raise WorkspaceError(f"Source path is not allowlisted: {relative}")
    return relative


def validate_remote_dir(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not re.fullmatch(r"/[A-Za-z0-9_./-]+", value)
        or not path.is_absolute() or len(path.parts) < 3 or path.as_posix() != value
        or any(part in {".", ".."} or _forbidden_component(part) for part in path.parts[1:])
    ):
        raise WorkspaceError("Remote directory must be an absolute, ordinary workspace path")
    return value


def _check_no_symlinks(path: Path) -> None:
    for part in (path, *path.parents):
        if part.is_symlink():
            raise WorkspaceError(f"Symlink paths are excluded: {path}")


def collect_source_files(project_root: Path) -> dict[str, bytes]:
    """Read only allowlisted regular files; cap reads before any cloud command."""
    root = project_root.resolve(strict=True)
    files: dict[str, bytes] = {}
    total = 0
    for pattern in SOURCE_GLOBS:
        for candidate in sorted(root.glob(pattern)):
            if candidate.is_dir() and not candidate.is_symlink():
                continue
            relative = validate_source_path(candidate.relative_to(root).as_posix())
            if relative in files:
                continue
            _check_no_symlinks(candidate)
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            with os.fdopen(os.open(candidate, flags), "rb") as source:
                metadata = os.fstat(source.fileno())
                if not stat.S_ISREG(metadata.st_mode):
                    raise WorkspaceError(f"Only regular source files can be transferred: {relative}")
                if metadata.st_size > MAX_FILE_BYTES:
                    raise WorkspaceError(f"File exceeds {MAX_FILE_BYTES} byte cap: {relative}")
                content = source.read(MAX_FILE_BYTES + 1)
            if len(content) > MAX_FILE_BYTES:
                raise WorkspaceError(f"File exceeds {MAX_FILE_BYTES} byte cap: {relative}")
            total += len(content)
            if total > MAX_TRANSFER_BYTES or len(files) >= MAX_FILES:
                raise WorkspaceError(f"Transfer exceeds {MAX_TRANSFER_BYTES} bytes or {MAX_FILES} files")
            files[relative] = content
    if not files:
        raise WorkspaceError("No allowlisted source files found")
    return dict(sorted(files.items()))


def receive_file(payload: dict) -> dict:
    """Remote receiver, also exercised locally by tests. Never executes content."""
    root = Path(validate_remote_dir(payload["root"]))
    relative = validate_source_path(payload["path"])
    content = base64.b64decode(payload["data"], validate=True)
    if len(content) > MAX_FILE_BYTES or len(content) != payload["size"]:
        raise WorkspaceError("Transferred file size is invalid")
    digest = hashlib.sha256(content).hexdigest()
    if digest != payload["sha256"]:
        raise WorkspaceError("Transferred file digest does not match")
    destination = root / relative
    _check_no_symlinks(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and not destination.is_file():
        raise WorkspaceError("Destination is not a regular file")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".broker-upload-", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(content)
        _check_no_symlinks(destination)
        os.replace(temporary, destination)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return {"path": relative, "bytes": len(content), "sha256": digest}


def receiver_script() -> str:
    # Send exactly the tested validators/receiver, with no local files or imports.
    header = (
        "import base64, hashlib, json, os, re, sys, tempfile\n"
        "from pathlib import Path, PurePosixPath\n"
        f"MAX_FILE_BYTES = {MAX_FILE_BYTES}\n"
    )
    source = "\n".join(inspect.getsource(function) for function in (
        WorkspaceError, _forbidden_component, validate_source_path,
        validate_remote_dir, _check_no_symlinks, receive_file,
    ))
    return header + source + (
        "\npayload = json.loads(base64.b64decode(sys.argv[1], validate=True))\n"
        f"print({RECEIPT_PREFIX!r} + json.dumps(receive_file(payload), sort_keys=True))\n"
    )


def find_daytona_cli(project_root: Path = PROJECT_ROOT) -> str:
    configured = os.environ.get("DAYTONA_CLI")
    if configured:
        resolved = shutil.which(configured)
        if not resolved:
            raise WorkspaceError("DAYTONA_CLI must name an executable file, without extra arguments")
        return resolved
    installed = shutil.which("daytona")
    if installed:
        return installed
    portable = (project_root / "../../work/bin/daytona").resolve()
    if portable.is_file() and os.access(portable, os.X_OK):
        return str(portable)
    raise WorkspaceError("Daytona CLI not found. Set DAYTONA_CLI to its executable path and run daytona login.")


class DaytonaWorkspace:
    def __init__(self, *, project_root: Path = PROJECT_ROOT, name: str = DEFAULT_NAME,
                 remote_dir: str = DEFAULT_REMOTE_DIR, timeout: int = 300, cli: str | None = None):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", name):
            raise WorkspaceError("Invalid sandbox name or ID")
        if timeout < 1:
            raise WorkspaceError("Timeout must be positive")
        self.project_root = Path(project_root)
        self.name = name
        self.remote_dir = validate_remote_dir(remote_dir)
        self.timeout = timeout
        self.cli = cli or find_daytona_cli(self.project_root)

    def _invoke(self, arguments: Sequence[str]) -> str:
        try:
            result = subprocess.run(
                [self.cli, *arguments], shell=False, capture_output=True,
                text=True, timeout=self.timeout + 15, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise WorkspaceError("Daytona command timed out; remote completion is unknown", 124) from exc
        except OSError as exc:
            raise WorkspaceError(f"Could not run the Daytona CLI: {exc.strerror or type(exc).__name__}") from exc
        if result.returncode:
            detail = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part.strip())[-4000:]
            code = result.returncode if 1 <= result.returncode <= 255 else 1
            raise WorkspaceError(f"Daytona command failed (exit {result.returncode}): {detail}", code)
        if result.stderr:
            print(result.stderr, file=sys.stderr, end="" if result.stderr.endswith("\n") else "\n")
        return result.stdout

    def _exec(self, arguments: Sequence[str], *, cwd: str | None = None) -> str:
        if any("\0" in argument for argument in arguments):
            raise WorkspaceError("Command arguments may not contain NUL bytes")
        flags = ["exec", self.name, "--timeout", str(self.timeout)]
        if cwd is not None:
            flags += ["--cwd", cwd]
        # Daytona accepts one shell-command string; quote each remote argument.
        # Local execution always uses an argument list and shell=False.
        return self._invoke([*flags, "--", shlex.join(arguments)])

    def status(self) -> str:
        return self._invoke(["info", self.name, "--format", "json"])

    def create(self) -> str:
        # The default snapshot determines resources. Daytona rejects explicit
        # CPU/memory overrides together with that snapshot.
        return self._invoke([
            "create", "--name", self.name, "--public=false", "--auto-stop", "10",
        ])

    def sync(self) -> dict:
        files = collect_source_files(self.project_root)
        script = receiver_script()
        completed = []
        for relative, content in files.items():
            digest = hashlib.sha256(content).hexdigest()
            payload = {"root": self.remote_dir, "path": relative,
                       "size": len(content), "sha256": digest,
                       "data": base64.b64encode(content).decode("ascii")}
            encoded = base64.b64encode(json.dumps(payload).encode()).decode("ascii")
            try:
                output = self._exec(["python3", "-c", script, encoded])
                receipts = [json.loads(line[len(RECEIPT_PREFIX):]) for line in output.splitlines()
                            if line.startswith(RECEIPT_PREFIX)]
                expected = {"path": relative, "bytes": len(content), "sha256": digest}
                if receipts != [expected]:
                    raise WorkspaceError("Remote upload acknowledgement is missing or mismatched")
            except WorkspaceError as exc:
                raise WorkspaceError(
                    f"Sync stopped at {relative}; {len(completed)} file(s) already updated. {exc}", exc.exit_code,
                ) from exc
            except (ValueError, TypeError) as exc:
                raise WorkspaceError(f"Invalid remote acknowledgement for {relative}; sync incomplete") from exc
            completed.append(relative)
        return {"sandbox": self.name, "directory": self.remote_dir,
                "files": completed, "bytes": sum(map(len, files.values()))}

    def run(self, mode: str, arguments: Sequence[str] = ()) -> str:
        if mode == "agent":
            mode = "review"
        if mode not in {"preflight", "review"}:
            raise WorkspaceError("Run mode must be preflight, review, or agent")
        return self._exec(["python3", "-m", "broker.cli", mode, *arguments], cwd=self.remote_dir)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("status", "create", "sync", "run", "preflight", "review", "agent"):
        child = commands.add_parser(command)
        child.add_argument("--name", default=DEFAULT_NAME, help="Existing sandbox name or ID")
        child.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
        child.add_argument("--remote-dir", default=DEFAULT_REMOTE_DIR)
        child.add_argument("--timeout", type=int, default=300, help="Remote command timeout in seconds")
        if command == "run":
            child.add_argument("mode", choices=("preflight", "review", "agent"))
        if command in {"run", "preflight", "review", "agent"}:
            child.add_argument("arguments", nargs=argparse.REMAINDER, help="Broker CLI arguments after --")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    options = build_parser().parse_args(argv)
    try:
        workspace = DaytonaWorkspace(project_root=options.project_root, name=options.name,
                                     remote_dir=options.remote_dir, timeout=options.timeout)
        if options.command in {"status", "create", "sync"}:
            output = getattr(workspace, options.command)()
        else:
            arguments = options.arguments
            if arguments[:1] == ["--"]:
                arguments = arguments[1:]
            output = workspace.run(options.mode if options.command == "run" else options.command, arguments)
        print(json.dumps(output, indent=2) if isinstance(output, dict) else output,
              end="" if isinstance(output, str) and output.endswith("\n") else "\n")
        return 0
    except (WorkspaceError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return exc.exit_code if isinstance(exc, WorkspaceError) else 1


if __name__ == "__main__":
    raise SystemExit(main())
