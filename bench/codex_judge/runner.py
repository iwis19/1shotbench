from __future__ import annotations

import hashlib
import contextlib
import json
import os
import signal
import shutil
import subprocess
import tempfile
import textwrap
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from bench.config import ROOT_DIR, load_project_env
from bench.llm_judge.report import write_artifacts
from bench.llm_judge.schemas import EvidencePacket, FeatureCheck, FeatureJudgment, WebEvalSummary


EVALS_DIR = ROOT_DIR / "evals"
CODEX_SKILL_DIR = ROOT_DIR / "judge_skills" / "web_judge"
BROWSER_HELPER = ROOT_DIR / "bench" / "llm_judge" / "browser.mjs"
DEFAULT_CODEX_MODEL = "gpt-5.6-luna"
DEFAULT_CODEX_REASONING_EFFORT = "low"
MUTATION_IGNORED_DIRS = {
    ".venv",
    "venv",
    "node_modules",
    "artifacts",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".cache",
    ".npm",
    ".pnpm-store",
    ".yarn",
    "logs",
    "playwright-report",
    "test-results",
    ".codex",
    ".next",
}
TOP_LEVEL_MUTATION_IGNORED_DIRS = {
    "work",
}
MUTATION_IGNORED_NAMES = {
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "poetry.lock",
    "Pipfile.lock",
    ".DS_Store",
    "next-env.d.ts",
}
TOP_LEVEL_MUTATION_IGNORED_NAMES = {
    "AGENTS.md",
    "CLAUDE.md",
}
MUTATION_IGNORED_SUFFIXES = {
    ".log",
    ".pid",
    ".pyc",
    ".jar",
    ".zip",
    ".whl",
    ".tgz",
}
MUTATION_IGNORED_PREFIX_SUFFIXES = (
    ("run.", ".txt"),
    ("eval.", ".txt"),
)


@dataclass
class CodexJudgeOptions:
    project_path: Path
    features_path: Path | None = None
    prd_path: Path | None = None
    base_url: str | None = None
    no_start: bool = False
    label: str = "codex-judge"
    eval_id: str | None = None
    codex_model: str | None = DEFAULT_CODEX_MODEL
    codex_command: str = "codex"
    judge_timeout_seconds: int = 900
    keep_judge_workspace: bool = False
    judge_workspace_root: Path | None = None
    codex_sandbox: str = "danger-full-access"
    search: bool = False


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _git_commit(root: Path) -> str | None:
    proc = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    return (proc.stdout or "").strip() or None


class CodexJudgeRunner:
    JUDGE_DISPLAY_NAME = "Codex"

    def __init__(self, root_dir: Path = ROOT_DIR):
        self.root_dir = root_dir.resolve()

    def preflight(self, options: CodexJudgeOptions) -> list[str]:
        errors: list[str] = []
        if not shutil.which(options.codex_command):
            errors.append(f"Codex CLI not found in PATH: {options.codex_command}")
        if not CODEX_SKILL_DIR.joinpath("SKILL.md").exists():
            errors.append(f"Missing Codex judge skill: {CODEX_SKILL_DIR / 'SKILL.md'}")
        if not BROWSER_HELPER.exists():
            errors.append(f"Missing browser helper: {BROWSER_HELPER}")
        helper_node_modules = BROWSER_HELPER.parent / "node_modules"
        if not helper_node_modules.exists():
            errors.append(
                "Playwright helper dependencies are missing. Run: "
                f"cd {BROWSER_HELPER.parent} && npm install && npx playwright install chromium"
            )
        return errors

    def run(self, options: CodexJudgeOptions) -> WebEvalSummary:
        from bench.llm_judge.features import load_features_file, write_features
        from bench.llm_judge.prd import load_prd_context

        errors = self.preflight(options)
        if errors:
            raise RuntimeError("\n".join(errors))

        project_path = options.project_path.resolve()
        if not project_path.exists():
            raise FileNotFoundError(f"Project path not found: {project_path}")

        if not options.features_path and not options.prd_path:
            raise ValueError("Pass --features, or pass --prd so Codex can generate features.")

        eval_id = options.eval_id or _make_eval_id()
        output_dir = EVALS_DIR / eval_id
        output_dir.mkdir(parents=True, exist_ok=True)

        features_path = options.features_path.resolve() if options.features_path else None
        canonical_features: list[FeatureCheck] = []
        generated_features = False
        if features_path:
            canonical_features, _ = load_features_file(features_path)

        started_at = _now_iso()
        judge_workspace = self._create_judge_workspace(options, project_path, output_dir)
        stdout_path = output_dir / "codex.stdout.log"
        stderr_path = output_dir / "codex.stderr.log"
        run_metadata_path = output_dir / "run.json"

        cleanup_workspace = not options.keep_judge_workspace
        try:
            app_copy = judge_workspace / "app"
            inputs_dir = judge_workspace / "inputs"
            skill_dir = judge_workspace / "judge_skill"
            work_dir = judge_workspace / "work"
            artifacts_dir = judge_workspace / "artifacts"
            for path in (inputs_dir, skill_dir, work_dir, artifacts_dir):
                path.mkdir(parents=True, exist_ok=True)

            shutil.copytree(project_path, app_copy, symlinks=True)
            shutil.copytree(CODEX_SKILL_DIR, skill_dir, dirs_exist_ok=True)

            prd_copy = None
            prd_context = None
            if options.prd_path:
                prd_copy = inputs_dir / Path(options.prd_path).name
                shutil.copy2(options.prd_path.resolve(), prd_copy)
                prd_context = load_prd_context(prd_copy)

            features_copy = None
            if features_path:
                features_copy = inputs_dir / features_path.name
                shutil.copy2(features_path, features_copy)

            before_manifest = _snapshot_mutation_manifest(app_copy)
            schema_path = judge_workspace / "codex-final-schema.json"
            schema_path.write_text(json.dumps(_codex_result_schema(), indent=2) + "\n", encoding="utf-8")
            final_path = judge_workspace / "codex-final.json"
            prompt = self._build_prompt(
                features_copy=features_copy,
                prd_copy=prd_copy,
                base_url=options.base_url,
                no_start=options.no_start,
            )

            command = _build_codex_exec_command(
                options=options,
                judge_workspace=judge_workspace,
                root_dir=self.root_dir,
                schema_path=schema_path,
                final_path=final_path,
                prompt=prompt,
            )
            run_metadata_path.write_text(
                json.dumps(
                    {
                        "judge": "codex",
                        "status": "starting",
                        "codex_command": command,
                        "codex_stdout_path": str(stdout_path),
                        "codex_stderr_path": str(stderr_path),
                        "structured_result_path": str(final_path),
                        "project_path": str(project_path),
                        "features_path": str(features_path) if features_path else None,
                        "prd_path": str(options.prd_path.resolve()) if options.prd_path else None,
                        "generated_features": generated_features,
                        "judge_workspace": str(judge_workspace),
                        "keep_judge_workspace": options.keep_judge_workspace,
                        "codex_sandbox": options.codex_sandbox,
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            proc = _run_codex_command(
                command,
                cwd=judge_workspace,
                env=load_project_env(),
                timeout=options.judge_timeout_seconds,
            )
            stdout_path.write_text(proc.stdout or "", encoding="utf-8")
            stderr_path.write_text(proc.stderr or "", encoding="utf-8")
            if proc.returncode != 0:
                raise RuntimeError(
                    f"Codex judge exited with code {proc.returncode}. See {stdout_path} and {stderr_path}."
                )
            if not final_path.exists():
                raise RuntimeError("Codex judge did not produce a final structured response.")

            raw_result = json.loads(final_path.read_text(encoding="utf-8"))
            after_manifest = _snapshot_mutation_manifest(app_copy)
            forbidden_mutations = _detect_forbidden_mutations(before_manifest, after_manifest)
            if forbidden_mutations:
                cleanup_workspace = False
                changed = ", ".join(forbidden_mutations[:12])
                raise RuntimeError(
                    "Codex judge modified forbidden app files in the disposable workspace: "
                    f"{changed}"
                )

            result_features = canonical_features
            if not result_features:
                generated = raw_result.get("features") or []
                result_features = _features_from_codex_result(generated)
                if not result_features:
                    raise RuntimeError("Codex judge did not return generated features.")
                generated_features = True
                features_path = output_dir / "generated-features.yaml"
                write_features(features_path, result_features)

            judgments, evidence_by_feature, notes = self._materialize_results(
                raw_result=raw_result,
                features=result_features,
                output_dir=output_dir,
            )

            summary = _build_summary(
                eval_id=eval_id,
                label=options.label,
                started_at=started_at,
                ended_at=_now_iso(),
                project_path=project_path,
                features_path=features_path,
                prd_path=options.prd_path.resolve() if options.prd_path else None,
                base_url=raw_result.get("base_url") or options.base_url or "",
                judgments=judgments,
                judge_model=options.codex_model or "codex-default",
                root_dir=self.root_dir,
                notes=notes,
            )
            write_artifacts(output_dir, summary=summary, evidence_by_feature=evidence_by_feature, judgments=judgments)
            run_metadata_path.write_text(
                json.dumps(
                    {
                        "judge": "codex",
                        "status": "completed",
                        "codex_command": command,
                        "codex_stdout_path": str(stdout_path),
                        "codex_stderr_path": str(stderr_path),
                        "structured_result_path": str(final_path),
                        "project_path": str(project_path),
                        "features_path": str(features_path) if features_path else None,
                        "prd_path": str(options.prd_path.resolve()) if options.prd_path else None,
                        "generated_features": generated_features,
                        "judge_workspace": str(judge_workspace),
                        "keep_judge_workspace": options.keep_judge_workspace,
                        "codex_sandbox": options.codex_sandbox,
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            return summary
        finally:
            _cleanup_judge_processes(judge_workspace)
            if cleanup_workspace:
                shutil.rmtree(judge_workspace, ignore_errors=True)

    def _create_judge_workspace(self, options: CodexJudgeOptions, project_path: Path, output_dir: Path) -> Path:
        if options.judge_workspace_root:
            root = options.judge_workspace_root.resolve()
            root.mkdir(parents=True, exist_ok=True)
            judge_workspace = root / f"codex-judge-{uuid.uuid4().hex[:8]}"
            judge_workspace.mkdir(parents=True, exist_ok=False)
            return judge_workspace
        if options.keep_judge_workspace:
            judge_workspace = output_dir / "judge-workspace"
            judge_workspace.mkdir(parents=True, exist_ok=False)
            return judge_workspace
        return Path(
            tempfile.mkdtemp(
                prefix=f"codex-judge-{project_path.name}-",
            )
        ).resolve()

    def _build_prompt(
        self,
        *,
        features_copy: Path | None,
        prd_copy: Path | None,
        base_url: str | None,
        no_start: bool,
    ) -> str:
        feature_line = (
            f"Use the checked-in feature file at `{features_copy.relative_to(features_copy.parent.parent)}` exactly."
            if features_copy
            else f"Generate the feature checks from `{prd_copy.relative_to(prd_copy.parent.parent)}`."
        )
        prd_line = (
            f"Use `{prd_copy.relative_to(prd_copy.parent.parent)}` as the product requirements reference."
            if prd_copy
            else "No PRD file was provided."
        )
        if no_start and base_url:
            app_line = f"The app is already running at `{base_url}`. Do not start a new server."
        elif no_start:
            app_line = "Do not start a new server. Use the provided running app context if available."
        else:
            app_line = "Start the app from `./app` if needed, using only documented setup/runtime steps."
        return textwrap.dedent(
            f"""
            You are the Codex judge for a 1ShotBench web-app evaluation.

            First read `./judge_skill/SKILL.md` and follow it strictly.

            Working locations:
            - App copy to evaluate: `./app`
            - Temporary browser jobs and temporary evidence: `./work`
            - Temporary judge artifacts: `./artifacts`
            - Inputs copied for this evaluation: `./inputs`
            - Final structured response: return it directly as your final message

            Core constraints:
            - Act as an evaluator, not a programmer; follow the judge skill for full safety, setup, speed, and evidence rules.
            - Judge only observed browser/runtime behavior, not source intent or code quality.
            - Do not inspect sibling agent workspaces or mutate outside `./app`, `./work`, and `./artifacts`.
            - Do not edit source-like app files to make the app run.
            - MUST use the Playwright helper at `{BROWSER_HELPER}` for browser evidence gathering. It already knows how to capture visible text, aria snapshots, console/network errors, same-origin API request statuses, interactive elements, and screenshots.
            - Keep command output concise. Do not print full evidence JSON, full app logs, full catalog dumps, or long file contents into the Codex transcript. Store full artifacts on disk and print only small summaries.

            Inputs:
            - {feature_line}
            - {prd_line}
            - {app_line}

            Evidence workflow:
            - Gather browser evidence and decide pass/fail/uncertain from evidence only.
            - Save screenshots under `./work`.
            - If the UI remains in a loading state, inspect helper API diagnostics and do one bounded longer wait, up to 30s total, before treating the feature as failed.
            - If startup fails completely, still return a verdict for every feature.

            Final response requirements:
            - Return only JSON matching the provided output schema.
            - Include one judgment object per feature.
            - Include an evidence object for each feature judgment.
            - Include concise notes about startup/setup issues when relevant.
            """
        ).strip()

    def _materialize_results(
        self,
        *,
        raw_result: dict[str, Any],
        features: list[FeatureCheck],
        output_dir: Path,
    ) -> tuple[list[FeatureJudgment], dict[str, EvidencePacket], str | None]:
        by_id = {feature.id: feature for feature in features}
        screenshots_dir = output_dir / "screenshots"
        screenshots_dir.mkdir(exist_ok=True)

        judgments: list[FeatureJudgment] = []
        evidence_by_feature: dict[str, EvidencePacket] = {}
        raw_judgments = raw_result.get("judgments") or []
        returned_by_id: dict[str, dict[str, Any]] = {}
        unknown_by_id: dict[str, dict[str, Any]] = {}
        for item in raw_judgments:
            if not isinstance(item, dict):
                continue
            feature_id = str(item.get("feature_id") or "").strip()
            if not feature_id:
                continue
            if feature_id in by_id:
                returned_by_id[feature_id] = item
            else:
                unknown_by_id[feature_id] = item

        notes_parts: list[str] = []
        remapped_feature_ids: list[str] = []
        extra_feature_ids: list[str] = []
        for feature_id, item in unknown_by_id.items():
            match = _closest_feature_id(feature_id, by_id.keys())
            if match and match not in returned_by_id:
                returned_by_id[match] = item
                remapped_feature_ids.append(f"{feature_id} -> {match}")
            else:
                extra_feature_ids.append(feature_id)

        if raw_result.get("notes"):
            notes_parts.append(str(raw_result["notes"]))
        if raw_result.get("startup_error"):
            notes_parts.append(f"Startup error: {raw_result['startup_error']}")
        if remapped_feature_ids:
            notes_parts.append(
                "Normalized judgment feature ids: " + ", ".join(sorted(remapped_feature_ids)[:8])
            )
        if extra_feature_ids:
            notes_parts.append(
                "Ignored judgments for unknown feature ids: " + ", ".join(sorted(extra_feature_ids)[:8])
            )

        for feature in features:
            item = returned_by_id.get(feature.id)
            if not item:
                judgments.append(
                    FeatureJudgment(
                        feature_id=feature.id,
                        verdict="uncertain",
                        confidence=0.0,
                        reason=f"{self.JUDGE_DISPLAY_NAME} judge did not return a verdict for this feature.",
                        evidence_used=["missing_judgment"],
                    )
                )
                evidence_by_feature[feature.id] = EvidencePacket(
                    feature_id=feature.id,
                    error=f"No evidence returned by {self.JUDGE_DISPLAY_NAME} judge.",
                )
                continue

            evidence = _evidence_from_codex_result(feature.id, item.get("evidence") or {})
            evidence.screenshot_path = _copy_screenshot(evidence.screenshot_path, screenshots_dir, feature.id)
            evidence_by_feature[feature.id] = evidence
            verdict = str(item.get("verdict") or "uncertain").lower()
            if verdict not in {"pass", "fail", "uncertain"}:
                verdict = "uncertain"
            judgments.append(
                FeatureJudgment(
                    feature_id=feature.id,
                    verdict=verdict,
                    confidence=float(item.get("confidence", 0.0) or 0.0),
                    reason=str(item.get("reason") or ""),
                    evidence_used=[str(x) for x in (item.get("evidence_used") or [])],
                )
            )

        notes = "\n".join(part for part in notes_parts if part).strip() or None
        return judgments, evidence_by_feature, notes


def _closest_feature_id(raw_id: str, candidates: Iterable[str]) -> str | None:
    matches = [candidate for candidate in candidates if _edit_distance_at_most_one(raw_id, candidate)]
    if len(matches) == 1:
        return matches[0]
    return None


def _edit_distance_at_most_one(left: str, right: str) -> bool:
    if left == right:
        return True
    if abs(len(left) - len(right)) > 1:
        return False

    if len(left) == len(right):
        return sum(1 for a, b in zip(left, right) if a != b) == 1

    shorter, longer = (left, right) if len(left) < len(right) else (right, left)
    i = j = edits = 0
    while i < len(shorter) and j < len(longer):
        if shorter[i] == longer[j]:
            i += 1
            j += 1
            continue
        edits += 1
        if edits > 1:
            return False
        j += 1
    return True


def _make_eval_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"codex-{stamp}-{uuid.uuid4().hex[:8]}"


def _build_codex_exec_command(
    *,
    options: CodexJudgeOptions,
    judge_workspace: Path,
    root_dir: Path,
    schema_path: Path,
    final_path: Path,
    prompt: str,
) -> list[str]:
    command = [
        options.codex_command,
        "-a",
        "never",
    ]
    if options.search:
        command.append("--search")
    command.extend(
        [
            "exec",
            "--ephemeral",
            "--skip-git-repo-check",
            "--ignore-user-config",
            "-c",
            f'model_reasoning_effort="{DEFAULT_CODEX_REASONING_EFFORT}"',
            "--sandbox",
            options.codex_sandbox,
            "-C",
            str(judge_workspace),
            "--add-dir",
            str(root_dir),
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(final_path),
        ]
    )
    if options.codex_model:
        command.extend(["--model", options.codex_model])
    command.append(prompt)
    return command


def _run_codex_command(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stdout_file:
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stderr_file:
            proc = subprocess.Popen(
                command,
                cwd=str(cwd),
                env=env,
                stdout=stdout_file,
                stderr=stderr_file,
                text=True,
                start_new_session=True,
            )
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                _terminate_process_group_id(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    _terminate_process_group_id(proc.pid, signal.SIGKILL)
                    proc.wait()
                stdout = _read_temp_output(stdout_file)
                stderr = _read_temp_output(stderr_file)
                raise subprocess.TimeoutExpired(command, timeout, output=stdout, stderr=stderr)
            finally:
                _terminate_process_group_id(proc.pid, signal.SIGTERM)
            return subprocess.CompletedProcess(
                command,
                proc.returncode,
                _read_temp_output(stdout_file),
                _read_temp_output(stderr_file),
            )


def _read_temp_output(file: Any) -> str:
    with contextlib.suppress(Exception):
        file.flush()
        file.seek(0)
        return file.read() or ""
    return ""


def _build_summary(
    *,
    eval_id: str,
    label: str,
    started_at: str,
    ended_at: str,
    project_path: Path,
    features_path: Path | None,
    prd_path: Path | None,
    base_url: str,
    judgments: list[FeatureJudgment],
    judge_model: str,
    root_dir: Path,
    notes: str | None,
) -> WebEvalSummary:
    total = len(judgments)
    passed = sum(1 for judgment in judgments if judgment.verdict == "pass")
    failed = sum(1 for judgment in judgments if judgment.verdict == "fail")
    uncertain = sum(1 for judgment in judgments if judgment.verdict == "uncertain")
    correctness = (passed / total * 100.0) if total else 0.0
    return WebEvalSummary(
        eval_id=eval_id,
        label=label,
        started_at=started_at,
        ended_at=ended_at,
        project_path=str(project_path),
        features_path=str(features_path) if features_path else "",
        prd_path=str(prd_path) if prd_path else None,
        base_url=base_url,
        total_features=total,
        passed=passed,
        failed=failed,
        uncertain=uncertain,
        correctness_pct=correctness,
        judgments=judgments,
        git_commit=_git_commit(root_dir),
        judge_model=f"codex:{judge_model}",
        notes=notes,
    )


def _features_from_codex_result(raw_features: list[Any]) -> list[FeatureCheck]:
    features: list[FeatureCheck] = []
    for item in raw_features:
        if not isinstance(item, dict):
            continue
        feature_id = str(item.get("id") or "").strip()
        title = str(item.get("title") or feature_id).strip()
        if not feature_id or not title:
            continue
        features.append(
            FeatureCheck(
                id=feature_id,
                title=title,
                description=str(item.get("description") or title),
                acceptance=str(item.get("acceptance") or title),
                steps=[],
            )
        )
    return features


def _evidence_from_codex_result(feature_id: str, raw: dict[str, Any]) -> EvidencePacket:
    interactive_elements: list[dict[str, Any]] = []
    for item in raw.get("interactive_elements") or []:
        if isinstance(item, dict):
            interactive_elements.append(item)
        else:
            interactive_elements.append({"text": str(item)})

    raw_checks = raw.get("checks") or {}
    if isinstance(raw_checks, dict):
        checks = raw_checks
    elif isinstance(raw_checks, list):
        checks = {"items": [str(item) for item in raw_checks]}
    else:
        checks = {"summary": str(raw_checks)}

    return EvidencePacket(
        feature_id=feature_id,
        url=raw.get("url"),
        page_title=raw.get("page_title"),
        visible_text=raw.get("visible_text"),
        aria_snapshot=raw.get("aria_snapshot"),
        screenshot_path=raw.get("screenshot_path"),
        interactive_elements=interactive_elements,
        console_errors=[str(x) for x in (raw.get("console_errors") or [])],
        network_errors=[str(x) for x in (raw.get("network_errors") or [])],
        action_log=[str(x) for x in (raw.get("action_log") or [])],
        checks=checks,
        error=raw.get("error"),
    )


def _copy_screenshot(path_value: str | None, screenshots_dir: Path, feature_id: str) -> str | None:
    if not path_value:
        return None
    source = Path(path_value)
    if not source.exists():
        return path_value
    target = screenshots_dir / f"{feature_id}{source.suffix or '.png'}"
    if source.resolve() != target.resolve():
        shutil.copy2(source, target)
    return str(target)


def _snapshot_mutation_manifest(root: Path) -> dict[str, str]:
    manifest: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if _ignore_for_mutation(rel):
            continue
        key = rel.as_posix()
        if path.is_symlink():
            manifest[key] = f"symlink:{os.readlink(path)}"
            continue
        if not path.is_file():
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        manifest[key] = digest
    return manifest


def _ignore_for_mutation(rel: Path) -> bool:
    parts = rel.parts
    if parts and parts[0] in TOP_LEVEL_MUTATION_IGNORED_DIRS:
        return True
    if len(parts) == 1 and rel.name in TOP_LEVEL_MUTATION_IGNORED_NAMES:
        return True
    if any(part in MUTATION_IGNORED_DIRS for part in parts):
        return True
    if rel.name in MUTATION_IGNORED_NAMES:
        return True
    if any(
        rel.name.startswith(prefix) and rel.name.endswith(suffix)
        for prefix, suffix in MUTATION_IGNORED_PREFIX_SUFFIXES
    ):
        return True
    if rel.suffix in MUTATION_IGNORED_SUFFIXES:
        return True
    return False


def _detect_forbidden_mutations(before: dict[str, str], after: dict[str, str]) -> list[str]:
    changed: list[str] = []
    all_keys = sorted(set(before) | set(after))
    for key in all_keys:
        if before.get(key) != after.get(key):
            changed.append(key)
    return changed


def _cleanup_judge_processes(judge_workspace: Path) -> None:
    pid_files = []
    for base in (judge_workspace / "work", judge_workspace / "app" / "work"):
        if base.exists():
            pid_files.extend(base.rglob("*.pid"))

    pids: list[int] = []
    for path in pid_files:
        try:
            pid = int(path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            continue
        if pid > 1 and pid != os.getpid():
            pids.append(pid)

    for pid in sorted(set(pids)):
        _terminate_pid(pid, signal.SIGTERM)
    if pids:
        time.sleep(0.5)
    for pid in sorted(set(pids)):
        if _pid_exists(pid):
            _terminate_pid(pid, signal.SIGKILL)


def _terminate_pid(pid: int, sig: signal.Signals) -> None:
    try:
        pgid = os.getpgid(pid)
    except OSError:
        pgid = None
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        if pgid and pgid not in {os.getpid(), os.getpgrp()}:
            os.killpg(pgid, sig)
            return
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.kill(pid, sig)


def _terminate_process_group_id(pgid: int, sig: signal.Signals) -> None:
    if pgid in {os.getpid(), os.getpgrp()}:
        return
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(pgid, sig)


def _pid_exists(pid: int) -> bool:
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, 0)
        return True
    return False


def _codex_result_schema() -> dict[str, Any]:
    feature_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "id": {"type": "string"},
            "title": {"type": "string"},
            "description": {"type": "string"},
            "acceptance": {"type": "string"},
        },
        "required": ["id", "title", "description", "acceptance"],
    }
    evidence_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "url": {"type": ["string", "null"]},
            "page_title": {"type": ["string", "null"]},
            "visible_text": {"type": ["string", "null"]},
            "aria_snapshot": {"type": ["string", "null"]},
            "screenshot_path": {"type": ["string", "null"]},
            "interactive_elements": {"type": "array", "items": {"type": "string"}},
            "console_errors": {"type": "array", "items": {"type": "string"}},
            "network_errors": {"type": "array", "items": {"type": "string"}},
            "action_log": {"type": "array", "items": {"type": "string"}},
            "checks": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "summary": {"type": "string"},
                    "details": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["summary", "details"],
            },
            "error": {"type": ["string", "null"]},
        },
        "required": [
            "url",
            "page_title",
            "visible_text",
            "aria_snapshot",
            "screenshot_path",
            "interactive_elements",
            "console_errors",
            "network_errors",
            "action_log",
            "checks",
            "error",
        ],
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "base_url": {"type": ["string", "null"]},
            "startup_error": {"type": ["string", "null"]},
            "notes": {"type": ["string", "null"]},
            "features": {"type": "array", "items": feature_schema},
            "judgments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "feature_id": {"type": "string"},
                        "verdict": {"type": "string", "enum": ["pass", "fail", "uncertain"]},
                        "confidence": {"type": "number"},
                        "reason": {"type": "string"},
                        "evidence_used": {"type": "array", "items": {"type": "string"}},
                        "evidence": evidence_schema,
                    },
                    "required": [
                        "feature_id",
                        "verdict",
                        "confidence",
                        "reason",
                        "evidence_used",
                        "evidence",
                    ],
                },
            },
        },
        "required": ["base_url", "startup_error", "notes", "features", "judgments"],
    }
