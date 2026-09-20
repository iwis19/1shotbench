from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from bench.config import ROOT_DIR
from bench.pi_judge.runner import (
    DEFAULT_PI_JUDGE_MODEL,
    DEFAULT_PI_JUDGE_PROVIDER,
    DEFAULT_PI_JUDGE_TOOLS,
    PiJudgeOptions,
    PiJudgeRunner,
)


DEFAULT_PI_CODEX_FAST_THINKING = "low"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="1ShotBench Pi judge for web apps")
    parser.add_argument("--project", required=True, help="Path to the coding agent workspace")
    parser.add_argument(
        "--features",
        help="YAML/JSON feature checks. If omitted, Pi generates them from --prd.",
    )
    parser.add_argument("--prd", help="Optional PRD markdown for judge context")
    parser.add_argument("--base-url", help="App URL when the server is already running")
    parser.add_argument("--no-start", action="store_true", help="Do not ask Pi to start the app")
    parser.add_argument("--label", default="pi-judge", help="Label for this evaluation run")
    parser.add_argument("--eval-id", help="Override output directory name under evals/")
    parser.add_argument("--pi-command", default="pi", help="Pi CLI executable")
    parser.add_argument(
        "--provider",
        default=DEFAULT_PI_JUDGE_PROVIDER,
        help=f"Pi provider to use for judging. Defaults to {DEFAULT_PI_JUDGE_PROVIDER}",
    )
    parser.add_argument(
        "--model",
        default=None,
        help=f"Pi model to use for judging. Defaults to {DEFAULT_PI_JUDGE_MODEL}",
    )
    parser.add_argument("--thinking", help="Optional Pi thinking setting")
    parser.add_argument(
        "--codex-fast",
        action="store_true",
        help=(
            "Use the openai-codex provider in fast mode. "
            f"Sets provider={DEFAULT_PI_JUDGE_PROVIDER}, model={DEFAULT_PI_JUDGE_MODEL} "
            f"when --model is omitted, and thinking={DEFAULT_PI_CODEX_FAST_THINKING} "
            "when --thinking is omitted."
        ),
    )
    parser.add_argument("--system-prompt", help="Optional Pi system prompt")
    parser.add_argument(
        "--append-system-prompt",
        action="append",
        default=[],
        help="Append an additional Pi system prompt. May be passed more than once.",
    )
    parser.add_argument(
        "--tools",
        default=",".join(DEFAULT_PI_JUDGE_TOOLS),
        help="Comma-separated Pi tools to enable for the judge",
    )
    parser.add_argument("--judge-timeout-seconds", type=int, default=900, help="Timeout for the Pi judge run")
    parser.add_argument("--keep-judge-workspace", action="store_true", help="Keep the disposable judge workspace after the run")
    parser.add_argument("--judge-workspace-root", help="Optional parent directory for the disposable judge workspace")
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

    tools = [tool.strip() for tool in args.tools.split(",") if tool.strip()]
    provider = DEFAULT_PI_JUDGE_PROVIDER if args.codex_fast else args.provider
    model = args.model or DEFAULT_PI_JUDGE_MODEL
    thinking = args.thinking
    if args.codex_fast and thinking is None:
        thinking = DEFAULT_PI_CODEX_FAST_THINKING
    runner = PiJudgeRunner()
    options = PiJudgeOptions(
        project_path=project,
        features_path=features,
        prd_path=prd,
        base_url=args.base_url,
        no_start=args.no_start,
        label=args.label,
        eval_id=args.eval_id,
        pi_command=args.pi_command,
        provider=provider,
        model=model,
        thinking=thinking,
        system_prompt=args.system_prompt,
        append_system_prompt=args.append_system_prompt,
        tools=tools,
        judge_timeout_seconds=args.judge_timeout_seconds,
        keep_judge_workspace=args.keep_judge_workspace,
        judge_workspace_root=judge_workspace_root,
    )

    try:
        summary = runner.run(options)
    except Exception as exc:
        print(f"pi-judge failed: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(summary.to_dict(), indent=2))
    print(f"\nArtifacts written to: {ROOT_DIR / 'evals' / summary.eval_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
