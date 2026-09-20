from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from bench.codex_judge.runner import DEFAULT_CODEX_MODEL, CodexJudgeOptions, CodexJudgeRunner
from bench.config import ROOT_DIR


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="1ShotBench Codex judge for web apps"
    )
    parser.add_argument("--project", required=True, help="Path to the coding agent workspace")
    parser.add_argument(
        "--features",
        help="YAML/JSON feature checks. If omitted, Codex generates them from --prd.",
    )
    parser.add_argument("--prd", help="Optional PRD markdown for judge context")
    parser.add_argument("--base-url", help="App URL when the server is already running")
    parser.add_argument("--no-start", action="store_true", help="Do not ask Codex to start the app")
    parser.add_argument("--label", default="codex-judge", help="Label for this evaluation run")
    parser.add_argument("--eval-id", help="Override output directory name under evals/")
    parser.add_argument(
        "--model",
        default=DEFAULT_CODEX_MODEL,
        help=f"Codex model to use for judging. Defaults to the lightest configured model: {DEFAULT_CODEX_MODEL}",
    )
    parser.add_argument("--codex-command", default="codex", help="Codex CLI executable")
    parser.add_argument("--judge-timeout-seconds", type=int, default=900, help="Timeout for the Codex judge run")
    parser.add_argument("--keep-judge-workspace", action="store_true", help="Keep the disposable judge workspace after the run")
    parser.add_argument("--judge-workspace-root", help="Optional parent directory for the disposable judge workspace")
    parser.add_argument(
        "--codex-sandbox",
        default="danger-full-access",
        choices=["read-only", "workspace-write", "danger-full-access"],
        help="Sandbox mode for the Codex judge process. Web judging defaults to danger-full-access so local servers and browsers can run.",
    )
    parser.add_argument("--search", action="store_true", help="Enable Codex live web search during judging")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    project = Path(args.project)
    if not project.is_absolute():
        project = ROOT_DIR / project

    features = Path(args.features) if args.features else None
    if features and not features.is_absolute():
        features = ROOT_DIR / features

    prd = Path(args.prd) if args.prd else None
    if prd and not prd.is_absolute():
        prd = ROOT_DIR / prd

    judge_workspace_root = Path(args.judge_workspace_root) if args.judge_workspace_root else None
    if judge_workspace_root and not judge_workspace_root.is_absolute():
        judge_workspace_root = ROOT_DIR / judge_workspace_root

    runner = CodexJudgeRunner()
    options = CodexJudgeOptions(
        project_path=project,
        features_path=features,
        prd_path=prd,
        base_url=args.base_url,
        no_start=args.no_start,
        label=args.label,
        eval_id=args.eval_id,
        codex_model=args.model,
        codex_command=args.codex_command,
        judge_timeout_seconds=args.judge_timeout_seconds,
        keep_judge_workspace=args.keep_judge_workspace,
        judge_workspace_root=judge_workspace_root,
        codex_sandbox=args.codex_sandbox,
        search=args.search,
    )

    try:
        summary = runner.run(options)
    except Exception as exc:
        print(f"codex-judge failed: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(summary.to_dict(), indent=2))
    print(f"\nArtifacts written to: {ROOT_DIR / 'evals' / summary.eval_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
