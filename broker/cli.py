"""Run deterministic checks or an explicitly requested live model review."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from broker import CaseValidationError, analyze_case
from broker.model import CodexInvoker, ModelError


def main() -> int:
    parser = argparse.ArgumentParser(description="Watcher → Matcher → Closer → Scribe. Drafts remain unsent.")
    parser.add_argument("mode", choices=("preflight", "review"))
    parser.add_argument("--case", type=Path, default=Path(__file__).resolve().parents[1] / "data/ap001.json")
    parser.add_argument("--review-at", help="ISO timestamp with timezone; retrospective effective-date review")
    parser.add_argument("--model", help="Optional Codex model override; otherwise use Codex's configured default")
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()
    try:
        case = json.loads(args.case.read_text())
        preflight = analyze_case(case, review_at=args.review_at)
        if args.mode == "preflight":
            packet = preflight
        else:
            from broker.pipeline import PipelineError, run_pipeline
            try:
                packet = run_pipeline(case, preflight, CodexInvoker(model=args.model, timeout=args.timeout),
                                      on_stage=lambda stage, result: print(
                                          f"{stage}: {result['status']} — handoff validated", file=sys.stderr))
            except PipelineError as exc:
                raise ModelError(str(exc)) from exc
        print(json.dumps(packet, indent=2, ensure_ascii=False))
        return 1 if packet.get("status") == "BLOCKED" else 0
    except (CaseValidationError, ModelError, ValueError, OSError) as exc:
        print(json.dumps({"status": "BLOCKED", "code": getattr(exc, "code", type(exc).__name__), "message": str(exc)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
