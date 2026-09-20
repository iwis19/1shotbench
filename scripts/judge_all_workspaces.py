#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import textwrap
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from bench.config import ROOT_DIR as BENCH_ROOT_DIR
from bench.llm_judge.schemas import WebEvalSummary

DEFAULT_PI_JUDGE_MODEL = "gpt-5.6-luna"
DEFAULT_PI_JUDGE_PROVIDER = "openai-codex"
DEFAULT_PI_CODEX_FAST_THINKING = "low"
DEFAULT_PI_JUDGE_TOOLS = ["read", "bash", "edit", "write", "grep", "find", "ls"]


EVALS_DIR = BENCH_ROOT_DIR / "evals"


@dataclass
class WorkspaceTarget:
    project_name: str
    workspace_name: str
    workspace_path: Path
    prd_path: Path
    features_path: Path | None


@dataclass
class BatchResult:
    project: str
    workspace: str
    status: str
    eval_id: str | None = None
    correctness_pct: float | None = None
    passed: int | None = None
    failed: int | None = None
    uncertain: int | None = None
    total_features: int | None = None
    notes: str | None = None
    failed_features: list[str] | None = None
    uncertain_features: list[str] | None = None
    artifact_dir: str | None = None
    elapsed_seconds: float | None = None
    error: str | None = None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a 1ShotBench judge across every workspace in every experiment project."
    )
    parser.add_argument(
        "--judge",
        choices=["pi", "codex", "llm"],
        default="pi",
        help="Judge harness to run. Defaults to pi.",
    )
    parser.add_argument(
        "--experiments-dir",
        default="experiments",
        help="Directory containing experiment projects. Defaults to experiments/.",
    )
    parser.add_argument(
        "--project",
        action="append",
        default=[],
        help="Only run projects with this name. May be passed more than once.",
    )
    parser.add_argument(
        "--workspace",
        action="append",
        default=[],
        help="Only run workspace directories with this name, with or without -workspace. May be passed more than once.",
    )
    parser.add_argument("--limit", type=int, help="Stop after this many discovered workspace runs.")
    parser.add_argument(
        "--list-only",
        action="store_true",
        help="Print discovered targets and exit without running a judge.",
    )
    parser.add_argument(
        "--continue-on-error",
        action="store_true",
        help="Continue after judge failures instead of stopping at the first failure.",
    )
    parser.add_argument(
        "--label-prefix",
        default=None,
        help="Optional label prefix. Defaults to all-<judge>-judge.",
    )
    parser.add_argument(
        "--batch-id",
        help="Override aggregate artifact directory name under evals/.",
    )
    parser.add_argument("--judge-timeout-seconds", type=int, default=900)
    parser.add_argument("--keep-judge-workspace", action="store_true")
    parser.add_argument("--judge-workspace-root", help="Optional parent directory for Codex/Pi judge workspaces.")

    parser.add_argument("--provider", default=DEFAULT_PI_JUDGE_PROVIDER, help="Pi judge provider.")
    parser.add_argument("--model", default=None, help="Pi or Codex judge model.")
    parser.add_argument("--thinking", help="Pi judge thinking setting.")
    parser.add_argument(
        "--codex-fast",
        action="store_true",
        help=(
            "Use the Pi judge with the openai-codex provider in fast mode. "
            f"Sets provider={DEFAULT_PI_JUDGE_PROVIDER}, model={DEFAULT_PI_JUDGE_MODEL} "
            f"when --model is omitted, and thinking={DEFAULT_PI_CODEX_FAST_THINKING} "
            "when --thinking is omitted."
        ),
    )
    parser.add_argument("--pi-command", default="pi", help="Pi CLI executable.")
    parser.add_argument(
        "--tools",
        default=",".join(DEFAULT_PI_JUDGE_TOOLS),
        help="Comma-separated Pi tools to enable.",
    )

    parser.add_argument("--codex-command", default="codex", help="Codex CLI executable.")
    parser.add_argument(
        "--codex-sandbox",
        default="danger-full-access",
        choices=["read-only", "workspace-write", "danger-full-access"],
    )
    parser.add_argument("--search", action="store_true", help="Enable Codex live web search.")

    parser.add_argument("--llm-judge-model", help="LLM judge model override.")
    parser.add_argument("--dry-run", action="store_true", help="LLM judge dry run.")
    parser.add_argument("--setup", choices=["auto", "never"], default="auto", help="LLM judge setup mode.")
    parser.add_argument("--no-agentic-evidence", action="store_true", help="Disable LLM agentic evidence.")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.codex_fast and args.judge != "pi":
        parser.error("--codex-fast only applies to --judge pi")
    targets = discover_targets(args)
    if args.limit is not None:
        targets = targets[: max(0, args.limit)]
    if not targets:
        print("No matching workspaces found.")
        return 1
    if args.list_only:
        print(f"Discovered {len(targets)} workspace(s):")
        for target in targets:
            feature_label = target.features_path.name if target.features_path else "generated from PRD"
            print(f"  - {target.project_name}/{target.workspace_name} ({feature_label})")
        return 0

    batch_id = args.batch_id or _make_batch_id(args.judge)
    batch_dir = EVALS_DIR / batch_id
    batch_dir.mkdir(parents=True, exist_ok=True)
    label_prefix = args.label_prefix or f"all-{args.judge}-judge"

    print(f"Judge: {args.judge}")
    print(f"Targets: {len(targets)} workspace(s)")
    print(f"Batch artifacts: {batch_dir}")
    print("")

    results: list[BatchResult] = []
    for index, target in enumerate(targets, start=1):
        label = f"{label_prefix}-{target.project_name}-{target.workspace_name.removesuffix('-workspace')}"
        print(f"[{index}/{len(targets)}] {target.project_name}/{target.workspace_name} ... ", end="", flush=True)
        started = time.monotonic()
        try:
            summary = run_target(args, target, label=label)
        except Exception as exc:
            elapsed_seconds = time.monotonic() - started
            result = BatchResult(
                project=target.project_name,
                workspace=target.workspace_name,
                status="error",
                elapsed_seconds=elapsed_seconds,
                error=str(exc),
            )
            results.append(result)
            print(f"ERROR after {_format_elapsed(elapsed_seconds)}: {_one_line(str(exc), 140)}")
            if not args.continue_on_error:
                write_batch_artifacts(batch_dir, args.judge, results)
                print("")
                print("Stopped at first error. Re-run with --continue-on-error to keep going.")
                return 1
            continue

        elapsed_seconds = time.monotonic() - started
        result = result_from_summary(target, summary, elapsed_seconds=elapsed_seconds)
        results.append(result)
        print(
            f"{summary.correctness_pct:.1f}% "
            f"({summary.passed} pass, {summary.failed} fail, {summary.uncertain} uncertain) "
            f"eval={summary.eval_id} "
            f"elapsed={_format_elapsed(elapsed_seconds)}"
        )
        if summary.notes:
            print(f"    notes: {_one_line(summary.notes, 180)}")

    write_batch_artifacts(batch_dir, args.judge, results)
    print_final_summary(results)
    print(f"\nAggregate artifacts written to: {batch_dir}")
    return 1 if any(result.status == "error" for result in results) else 0


def discover_targets(args: argparse.Namespace) -> list[WorkspaceTarget]:
    experiments_dir = Path(args.experiments_dir)
    if not experiments_dir.is_absolute():
        experiments_dir = BENCH_ROOT_DIR / experiments_dir

    project_filter = set(args.project)
    workspace_filter = {_normalize_workspace_name(value) for value in args.workspace}

    targets: list[WorkspaceTarget] = []
    for project_dir in sorted(experiments_dir.iterdir() if experiments_dir.exists() else []):
        if not project_dir.is_dir():
            continue
        if project_filter and project_dir.name not in project_filter:
            continue
        prd_path = project_dir / "PRD.md"
        if not prd_path.exists():
            continue
        features_path = project_dir / "features.yaml"
        if not features_path.exists():
            features_path = None
        for workspace_path in sorted(project_dir.glob("*-workspace")):
            if not workspace_path.is_dir():
                continue
            if workspace_filter and workspace_path.name not in workspace_filter:
                continue
            targets.append(
                WorkspaceTarget(
                    project_name=project_dir.name,
                    workspace_name=workspace_path.name,
                    workspace_path=workspace_path,
                    prd_path=prd_path,
                    features_path=features_path,
                )
            )
    return targets


def run_target(args: argparse.Namespace, target: WorkspaceTarget, *, label: str) -> WebEvalSummary:
    judge_workspace_root = Path(args.judge_workspace_root) if args.judge_workspace_root else None
    if judge_workspace_root and not judge_workspace_root.is_absolute():
        judge_workspace_root = BENCH_ROOT_DIR / judge_workspace_root

    if args.judge == "pi":
        from bench.pi_judge.runner import PiJudgeOptions, PiJudgeRunner

        tools = [tool.strip() for tool in args.tools.split(",") if tool.strip()]
        provider = DEFAULT_PI_JUDGE_PROVIDER if args.codex_fast else args.provider
        model = args.model or DEFAULT_PI_JUDGE_MODEL
        thinking = args.thinking
        if args.codex_fast and thinking is None:
            thinking = DEFAULT_PI_CODEX_FAST_THINKING
        return PiJudgeRunner().run(
            PiJudgeOptions(
                project_path=target.workspace_path,
                features_path=target.features_path,
                prd_path=target.prd_path,
                label=label,
                pi_command=args.pi_command,
                provider=provider,
                model=model,
                thinking=thinking,
                tools=tools,
                judge_timeout_seconds=args.judge_timeout_seconds,
                keep_judge_workspace=args.keep_judge_workspace,
                judge_workspace_root=judge_workspace_root,
            )
        )

    if args.judge == "codex":
        from bench.codex_judge.runner import CodexJudgeOptions, CodexJudgeRunner

        return CodexJudgeRunner().run(
            CodexJudgeOptions(
                project_path=target.workspace_path,
                features_path=target.features_path,
                prd_path=target.prd_path,
                label=label,
                codex_model=args.model,
                codex_command=args.codex_command,
                judge_timeout_seconds=args.judge_timeout_seconds,
                keep_judge_workspace=args.keep_judge_workspace,
                judge_workspace_root=judge_workspace_root,
                codex_sandbox=args.codex_sandbox,
                search=args.search,
            )
        )

    from bench.llm_judge.runner import WebEvalOptions, WebEvalRunner

    return WebEvalRunner().run(
        WebEvalOptions(
            project_path=target.workspace_path,
            features_path=target.features_path,
            prd_path=target.prd_path,
            label=label,
            dry_run=args.dry_run,
            judge_model=args.llm_judge_model,
            setup_mode=args.setup,
            agentic_evidence=not args.no_agentic_evidence,
        )
    )


def result_from_summary(
    target: WorkspaceTarget,
    summary: WebEvalSummary,
    *,
    elapsed_seconds: float,
) -> BatchResult:
    failed_features = [judgment.feature_id for judgment in summary.judgments if judgment.verdict == "fail"]
    uncertain_features = [judgment.feature_id for judgment in summary.judgments if judgment.verdict == "uncertain"]
    return BatchResult(
        project=target.project_name,
        workspace=target.workspace_name,
        status="completed",
        eval_id=summary.eval_id,
        correctness_pct=summary.correctness_pct,
        passed=summary.passed,
        failed=summary.failed,
        uncertain=summary.uncertain,
        total_features=summary.total_features,
        notes=summary.notes,
        failed_features=failed_features,
        uncertain_features=uncertain_features,
        artifact_dir=str(EVALS_DIR / summary.eval_id),
        elapsed_seconds=elapsed_seconds,
    )


def write_batch_artifacts(batch_dir: Path, judge: str, results: list[BatchResult]) -> None:
    payload = {
        "judge": judge,
        "written_at": datetime.now(timezone.utc).isoformat(),
        "totals": compute_totals(results),
        "results": [asdict(result) for result in results],
    }
    (batch_dir / "summary.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    (batch_dir / "summary.md").write_text(render_batch_markdown(payload), encoding="utf-8")


def compute_totals(results: list[BatchResult]) -> dict[str, int | float]:
    completed = [result for result in results if result.status == "completed"]
    total_features = sum(result.total_features or 0 for result in completed)
    passed = sum(result.passed or 0 for result in completed)
    failed = sum(result.failed or 0 for result in completed)
    uncertain = sum(result.uncertain or 0 for result in completed)
    return {
        "workspaces": len(results),
        "completed": len(completed),
        "errors": sum(1 for result in results if result.status == "error"),
        "total_features": total_features,
        "passed": passed,
        "failed": failed,
        "uncertain": uncertain,
        "correctness_pct": (passed / total_features * 100.0) if total_features else 0.0,
    }


def render_batch_markdown(payload: dict) -> str:
    totals = payload["totals"]
    lines = [
        f"# All Workspace Judge Batch: {payload['judge']}",
        "",
        f"- **Written:** {payload['written_at']}",
        f"- **Workspaces:** {totals['workspaces']} ({totals['completed']} completed, {totals['errors']} errors)",
        f"- **Feature correctness:** {totals['correctness_pct']:.1f}% ({totals['passed']}/{totals['total_features']} passed)",
        f"- **Failed:** {totals['failed']}",
        f"- **Uncertain:** {totals['uncertain']}",
        "",
        "| Project | Workspace | Status | Elapsed | Score | Failed | Uncertain | Notes |",
        "| --- | --- | --- | ---: | ---: | --- | --- | --- |",
    ]
    for item in payload["results"]:
        if item["status"] == "completed":
            score = f"{item['correctness_pct']:.1f}%"
            failed = ", ".join(item.get("failed_features") or [])
            uncertain = ", ".join(item.get("uncertain_features") or [])
            notes = _one_line(item.get("notes") or "", 120)
        else:
            score = "n/a"
            failed = ""
            uncertain = ""
            notes = _one_line(item.get("error") or "", 120)
        lines.append(
            f"| {item['project']} | {item['workspace']} | {item['status']} | "
            f"{_format_elapsed(item.get('elapsed_seconds'))} | {score} | {failed} | {uncertain} | {notes} |"
        )
    return "\n".join(lines) + "\n"


def print_final_summary(results: list[BatchResult]) -> None:
    totals = compute_totals(results)
    print("")
    print("Final summary")
    print(
        f"  Workspaces: {totals['workspaces']} "
        f"({totals['completed']} completed, {totals['errors']} errors)"
    )
    print(
        f"  Feature score: {totals['correctness_pct']:.1f}% "
        f"({totals['passed']} pass, {totals['failed']} fail, {totals['uncertain']} uncertain)"
    )

    failed = [result for result in results if result.failed_features]
    uncertain = [result for result in results if result.uncertain_features]
    errors = [result for result in results if result.status == "error"]
    if failed:
        print("  Failed features:")
        for result in failed:
            print(f"    - {result.project}/{result.workspace}: {', '.join(result.failed_features or [])}")
    if uncertain:
        print("  Uncertain features:")
        for result in uncertain:
            print(f"    - {result.project}/{result.workspace}: {', '.join(result.uncertain_features or [])}")
    if errors:
        print("  Errors:")
        for result in errors:
            print(f"    - {result.project}/{result.workspace}: {_one_line(result.error or '', 140)}")


def _make_batch_id(judge: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"batch-{judge}-{stamp}-{uuid.uuid4().hex[:8]}"


def _normalize_workspace_name(value: str) -> str:
    return value if value.endswith("-workspace") else f"{value}-workspace"


def _one_line(value: str, limit: int) -> str:
    compact = " ".join(str(value).split())
    return textwrap.shorten(compact, width=limit, placeholder="...") if compact else ""


def _format_elapsed(seconds: float | None) -> str:
    if seconds is None:
        return "n/a"
    total = max(0, int(round(seconds)))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{secs:02d}s"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


if __name__ == "__main__":
    raise SystemExit(main())
