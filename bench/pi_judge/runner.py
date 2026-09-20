from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import tempfile
import textwrap
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from bench.codex_judge.runner import (
    BROWSER_HELPER,
    CODEX_SKILL_DIR,
    EVALS_DIR,
    CodexJudgeRunner,
    _build_summary,
    _codex_result_schema,
    _cleanup_judge_processes,
    _detect_forbidden_mutations,
    _features_from_codex_result,
    _git_commit,
    _now_iso,
    _read_temp_output,
    _snapshot_mutation_manifest,
    _terminate_process_group_id,
)
from bench.config import ROOT_DIR, load_project_env
from bench.llm_judge.report import write_artifacts
from bench.llm_judge.schemas import FeatureCheck, WebEvalSummary
from bench.schemas import WorkspaceConfig


DEFAULT_PI_JUDGE_MODEL = "gpt-5.6-luna"
DEFAULT_PI_JUDGE_PROVIDER = "openai-codex"
DEFAULT_PI_JUDGE_TOOLS = ["read", "bash", "edit", "write", "grep", "find", "ls"]


@dataclass
class PiJudgeOptions:
    project_path: Path
    features_path: Path | None = None
    prd_path: Path | None = None
    base_url: str | None = None
    no_start: bool = False
    label: str = "pi-judge"
    eval_id: str | None = None
    pi_command: str = "pi"
    provider: str | None = DEFAULT_PI_JUDGE_PROVIDER
    model: str | None = DEFAULT_PI_JUDGE_MODEL
    thinking: str | None = None
    system_prompt: str | None = None
    append_system_prompt: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=lambda: list(DEFAULT_PI_JUDGE_TOOLS))
    judge_timeout_seconds: int = 900
    keep_judge_workspace: bool = False
    judge_workspace_root: Path | None = None


class PiJudgeRunner(CodexJudgeRunner):
    JUDGE_DISPLAY_NAME = "Pi"

    def preflight(self, options: PiJudgeOptions) -> list[str]:
        errors: list[str] = []
        if not shutil.which(options.pi_command):
            errors.append(f"Pi CLI not found in PATH: {options.pi_command}")
        if not CODEX_SKILL_DIR.joinpath("SKILL.md").exists():
            errors.append(f"Missing web judge skill: {CODEX_SKILL_DIR / 'SKILL.md'}")
        if not BROWSER_HELPER.exists():
            errors.append(f"Missing browser helper: {BROWSER_HELPER}")
        helper_node_modules = BROWSER_HELPER.parent / "node_modules"
        if not helper_node_modules.exists():
            errors.append(
                "Playwright helper dependencies are missing. Run: "
                f"cd {BROWSER_HELPER.parent} && npm install && npx playwright install chromium"
            )
        return errors

    def run(self, options: PiJudgeOptions) -> WebEvalSummary:
        from bench.llm_judge.features import load_features_file, write_features
        from bench.llm_judge.prd import load_prd_context

        errors = self.preflight(options)
        if errors:
            raise RuntimeError("\n".join(errors))

        project_path = options.project_path.resolve()
        if not project_path.exists():
            raise FileNotFoundError(f"Project path not found: {project_path}")

        if not options.features_path and not options.prd_path:
            raise ValueError("Pass --features, or pass --prd so Pi can generate features.")

        eval_id = options.eval_id or _make_pi_eval_id()
        output_dir = EVALS_DIR / eval_id
        output_dir.mkdir(parents=True, exist_ok=True)

        features_path = options.features_path.resolve() if options.features_path else None
        canonical_features: list[FeatureCheck] = []
        generated_features = False
        if features_path:
            canonical_features, _ = load_features_file(features_path)

        started_at = _now_iso()
        judge_workspace = self._create_judge_workspace(options, project_path, output_dir)
        stdout_path = output_dir / "pi.stdout.log"
        stderr_path = output_dir / "pi.stderr.log"
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
            if options.prd_path:
                prd_copy = inputs_dir / Path(options.prd_path).name
                shutil.copy2(options.prd_path.resolve(), prd_copy)
                load_prd_context(prd_copy)

            features_copy = None
            if features_path:
                features_copy = inputs_dir / features_path.name
                shutil.copy2(features_path, features_copy)

            before_manifest = _snapshot_mutation_manifest(app_copy)
            schema_path = judge_workspace / "pi-final-schema.json"
            schema_path.write_text(json.dumps(_codex_result_schema(), indent=2) + "\n", encoding="utf-8")
            final_path = judge_workspace / "pi-final.json"
            prompt = self._build_pi_prompt(
                features_copy=features_copy,
                prd_copy=prd_copy,
                base_url=options.base_url,
                no_start=options.no_start,
                schema_path=schema_path,
                final_path=final_path,
            )

            command = _build_pi_exec_command(options=options, prompt=prompt)
            run_metadata_path.write_text(
                json.dumps(
                    {
                        "judge": "pi",
                        "status": "starting",
                        "pi_command": command,
                        "pi_stdout_path": str(stdout_path),
                        "pi_stderr_path": str(stderr_path),
                        "structured_result_path": str(final_path),
                        "structured_result_schema_path": str(schema_path),
                        "project_path": str(project_path),
                        "features_path": str(features_path) if features_path else None,
                        "prd_path": str(options.prd_path.resolve()) if options.prd_path else None,
                        "generated_features": generated_features,
                        "judge_workspace": str(judge_workspace),
                        "keep_judge_workspace": options.keep_judge_workspace,
                        "provider": options.provider,
                        "model": options.model,
                        "thinking": options.thinking,
                        "tools": options.tools,
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            try:
                proc = _run_pi_command(
                    command,
                    cwd=judge_workspace,
                    env=load_project_env(),
                    timeout=options.judge_timeout_seconds,
                )
            except subprocess.TimeoutExpired as exc:
                cleanup_workspace = False
                stdout_path.write_text(_process_output_to_text(exc.stdout), encoding="utf-8")
                stderr = _process_output_to_text(exc.stderr)
                if stderr and not stderr.endswith("\n"):
                    stderr += "\n"
                stderr += f"Pi judge timed out after {options.judge_timeout_seconds}s.\n"
                stderr_path.write_text(stderr, encoding="utf-8")
                run_metadata_path.write_text(
                    json.dumps(
                        {
                            "judge": "pi",
                            "status": "timed_out",
                            "pi_command": command,
                            "pi_stdout_path": str(stdout_path),
                            "pi_stderr_path": str(stderr_path),
                            "structured_result_path": str(final_path),
                            "structured_result_schema_path": str(schema_path),
                            "project_path": str(project_path),
                            "features_path": str(features_path) if features_path else None,
                            "prd_path": str(options.prd_path.resolve()) if options.prd_path else None,
                            "generated_features": generated_features,
                            "judge_workspace": str(judge_workspace),
                            "keep_judge_workspace": True,
                            "provider": options.provider,
                            "model": options.model,
                            "thinking": options.thinking,
                            "tools": options.tools,
                            "timeout_seconds": options.judge_timeout_seconds,
                        },
                        indent=2,
                    )
                    + "\n",
                    encoding="utf-8",
                )
                raise RuntimeError(
                    f"Pi judge timed out after {options.judge_timeout_seconds}s. "
                    f"See {stdout_path}, {stderr_path}, and preserved workspace {judge_workspace}."
                ) from exc
            stdout_path.write_text(proc.stdout or "", encoding="utf-8")
            stderr_path.write_text(proc.stderr or "", encoding="utf-8")
            if proc.returncode != 0:
                raise RuntimeError(f"Pi judge exited with code {proc.returncode}. See {stdout_path} and {stderr_path}.")
            if not final_path.exists():
                raise RuntimeError("Pi judge did not write the final structured result file.")

            raw_result = json.loads(final_path.read_text(encoding="utf-8"))
            after_manifest = _snapshot_mutation_manifest(app_copy)
            forbidden_mutations = _detect_forbidden_mutations(before_manifest, after_manifest)
            if forbidden_mutations:
                cleanup_workspace = False
                changed = ", ".join(forbidden_mutations[:12])
                raise RuntimeError(f"Pi judge modified forbidden app files in the disposable workspace: {changed}")

            result_features = canonical_features
            if not result_features:
                generated = raw_result.get("features") or []
                result_features = _features_from_codex_result(generated)
                if not result_features:
                    raise RuntimeError("Pi judge did not return generated features.")
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
                judge_model=_pi_model_label(options),
                root_dir=self.root_dir,
                notes=notes,
            )
            summary.judge_model = f"pi:{_pi_model_label(options)}"
            summary.git_commit = _git_commit(self.root_dir)
            write_artifacts(output_dir, summary=summary, evidence_by_feature=evidence_by_feature, judgments=judgments)
            run_metadata_path.write_text(
                json.dumps(
                    {
                        "judge": "pi",
                        "status": "completed",
                        "pi_command": command,
                        "pi_stdout_path": str(stdout_path),
                        "pi_stderr_path": str(stderr_path),
                        "structured_result_path": str(final_path),
                        "structured_result_schema_path": str(schema_path),
                        "project_path": str(project_path),
                        "features_path": str(features_path) if features_path else None,
                        "prd_path": str(options.prd_path.resolve()) if options.prd_path else None,
                        "generated_features": generated_features,
                        "judge_workspace": str(judge_workspace),
                        "keep_judge_workspace": options.keep_judge_workspace,
                        "provider": options.provider,
                        "model": options.model,
                        "thinking": options.thinking,
                        "tools": options.tools,
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

    def _create_judge_workspace(self, options: PiJudgeOptions, project_path: Path, output_dir: Path) -> Path:
        if options.judge_workspace_root:
            root = options.judge_workspace_root.resolve()
            root.mkdir(parents=True, exist_ok=True)
            judge_workspace = root / f"pi-judge-{uuid.uuid4().hex[:8]}"
            judge_workspace.mkdir(parents=True, exist_ok=False)
            return judge_workspace
        if options.keep_judge_workspace:
            judge_workspace = output_dir / "judge-workspace"
            judge_workspace.mkdir(parents=True, exist_ok=False)
            return judge_workspace
        return Path(tempfile.mkdtemp(prefix=f"pi-judge-{project_path.name}-")).resolve()

    def _build_pi_prompt(
        self,
        *,
        features_copy: Path | None,
        prd_copy: Path | None,
        base_url: str | None,
        no_start: bool,
        schema_path: Path,
        final_path: Path,
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
            You are the Pi judge for a 1ShotBench web-app evaluation.

            First read `./judge_skill/SKILL.md` and follow it strictly.

            Working locations:
            - App copy to evaluate: `./app`
            - Temporary browser jobs and temporary evidence: `./work`
            - Temporary judge artifacts: `./artifacts`
            - Inputs copied for this evaluation: `./inputs`
            - Final structured response path: `{final_path.relative_to(final_path.parent)}`
            - Final structured response schema: `{schema_path.relative_to(schema_path.parent)}`

            Core constraints:
            - Act as an evaluator, not a programmer; follow the judge skill for full safety, setup, speed, and evidence rules.
            - Judge only observed browser/runtime behavior, not source intent or code quality.
            - Do not inspect sibling agent workspaces or mutate outside `./app`, `./work`, and `./artifacts`.
            - Do not edit source-like app files to make the app run.
            - MUST use the Playwright helper at `{BROWSER_HELPER}` for browser evidence gathering. It already knows how to capture visible text, aria snapshots, console/network errors, same-origin API request statuses, interactive elements, and screenshots.
            - Keep command output concise. Do not print full evidence JSON, full app logs, full catalog dumps, or long file contents into the Pi transcript. Store full artifacts on disk and print only small summaries.

            Inputs:
            - {feature_line}
            - {prd_line}
            - {app_line}

            Evidence workflow:
            - Gather browser evidence and decide pass/fail/uncertain from evidence only.
            - Save screenshots under `./work`.
            - Use canonical feature ids verbatim from the feature file; do not rename or correct spelling.
            - Prefer one initial browser pass and at most one bounded follow-up per feature.
            - Do not rerun successful evidence or duplicate browser/API checks once captured evidence proves the behavior.
            - If the UI remains in a loading state, inspect helper API diagnostics and do one bounded longer wait, up to 30s total, before treating the feature as failed.
            - If using an alternate port, verify the response belongs to the current `./app` copy. If logs, error pages, paths, or page identity reference a different temp workspace or wrong app root, treat that listener as stale/wrong and use a fresh port before judging.
            - If startup fails completely, still return a verdict for every feature.

            Final response requirements:
            - Write only JSON matching the provided output schema to `{final_path.relative_to(final_path.parent)}`.
            - Include one judgment object per feature.
            - Include an evidence object for each feature judgment.
            - Include concise notes about startup/setup issues when relevant.
            - After writing the file, print only a short confirmation and do not paste the JSON into the transcript.
            """
        ).strip()


def _make_pi_eval_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"pi-{stamp}-{uuid.uuid4().hex[:8]}"


def _build_pi_exec_command(*, options: PiJudgeOptions, prompt: str) -> list[str]:
    workspace = WorkspaceConfig(
        key="pi-judge",
        name="Pi Judge",
        path=".",
        model=options.model or "",
        provider=options.provider,
        thinking=options.thinking,
        system_prompt=options.system_prompt,
        append_system_prompt=options.append_system_prompt,
        tools=options.tools,
    )
    command = workspace.pi_args()
    command[0] = options.pi_command
    command.append(prompt)
    return command


def _run_pi_command(
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
            except subprocess.TimeoutExpired as exc:
                _terminate_process_group_id(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    _terminate_process_group_id(proc.pid, signal.SIGKILL)
                    proc.wait()
                exc.stdout = _merge_timeout_output(exc.stdout, _read_temp_output(stdout_file))
                exc.stderr = _merge_timeout_output(exc.stderr, _read_temp_output(stderr_file))
                raise
            finally:
                _terminate_process_group_id(proc.pid, signal.SIGTERM)
            return subprocess.CompletedProcess(
                command,
                proc.returncode,
                _read_temp_output(stdout_file),
                _read_temp_output(stderr_file),
            )


def _terminate_process_group(pid: int, sig: signal.Signals) -> None:
    try:
        pgid = os.getpgid(pid)
    except OSError:
        return
    if pgid in {os.getpid(), os.getpgrp()}:
        return
    try:
        os.killpg(pgid, sig)
        if sig == signal.SIGTERM:
            time.sleep(0.2)
    except (ProcessLookupError, PermissionError, OSError):
        return


def _merge_timeout_output(original: str | bytes | None, after_kill: str | bytes | None) -> str:
    original_text = _process_output_to_text(original)
    after_text = _process_output_to_text(after_kill)
    if not original_text:
        return after_text
    if not after_text:
        return original_text
    if original_text.endswith(after_text):
        return original_text
    return original_text + after_text


def _process_output_to_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _pi_model_label(options: PiJudgeOptions) -> str:
    if options.provider and options.model:
        return f"{options.provider}/{options.model}"
    if options.model:
        return options.model
    if options.provider:
        return options.provider
    return "default"
