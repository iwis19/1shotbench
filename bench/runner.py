from __future__ import annotations

import asyncio
import contextlib
import csv
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

from bench.config import RUNS_DIR, discover_shared_task_files, load_project_env
from bench.metrics import extract_inline_token_usage
from bench.sandbox import apply_workspace_sandbox, sandbox_preflight_error
from bench.schemas import BenchmarkSummary, RunJobResult, TokenMetrics, WorkspaceConfig


EventCallback = Callable[[dict], Awaitable[None]]
SUBPROCESS_STREAM_LIMIT_BYTES = 8 * 1024 * 1024


@dataclass
class RunnerOptions:
    prompt: str
    selected_models: list[str]
    task_dir: str | None = None
    mode: str = "parallel"
    max_concurrency: int = 2
    timeout_seconds: int = 1800
    retries: int = 0
    label: str = "benchmark"
    warmup: bool = False
    rewrite_task_files: bool = True
    rewrite_timeout_seconds: int = 600
    run_id: str | None = None


@dataclass
class TaskRewriteResult:
    status: str
    artifact_path: Path
    stdout_path: Path
    stderr_path: Path
    events_path: Path
    command: list[str]
    exit_code: int | None = None
    error: str | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(ts: datetime) -> str:
    return ts.isoformat()


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


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


class BenchmarkRunner:
    def __init__(self, root_dir: Path, workspaces: dict[str, WorkspaceConfig], task_dir: str | Path | None = None):
        self.root_dir = root_dir
        self.workspaces = workspaces
        self.task_dir = Path(task_dir).resolve() if task_dir else None

    def preflight(self, selected_models: list[str]) -> list[str]:
        errors: list[str] = []
        missing = [model for model in selected_models if model not in self.workspaces]
        if missing:
            errors.append(f"Unknown model keys: {', '.join(missing)}")

        for model in selected_models:
            workspace = self.workspaces.get(model)
            if workspace and not Path(workspace.path).exists():
                errors.append(f"Workspace not found: {workspace.path}")

        if not shutil.which("pi"):
            errors.append("`pi` command is not available in PATH.")
        sandbox_error = sandbox_preflight_error()
        if sandbox_error:
            errors.append(sandbox_error)
        for skill in sorted({skill for model in selected_models for skill in self.workspaces.get(model, WorkspaceConfig("", "", "", "")).required_skills}):
            if not self._skill_available(skill):
                errors.append(
                    f"Required skill `{skill}` is not installed. Install it under `.agents/skills/`, "
                    "`~/.pi/agent/skills/`, or another Pi skill location before running."
                )

        return errors

    def _skill_available(self, skill_name: str) -> bool:
        search_roots = [
            self.root_dir / ".agents" / "skills",
            self.root_dir / ".pi" / "skills",
            Path.home() / ".pi" / "agent" / "skills",
            Path.home() / ".agents" / "skills",
        ]
        for root in search_roots:
            if not root.exists():
                continue
            direct = root / skill_name / "SKILL.md"
            if direct.exists():
                return True
            for skill_file in root.rglob("SKILL.md"):
                try:
                    text = skill_file.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                if re.search(rf"(?m)^name:\s*{re.escape(skill_name)}\s*$", text):
                    return True
        return False

    def sync_shared_task_files(self) -> None:
        task_files = discover_shared_task_files(self._task_dir())
        if not task_files:
            return
        for workspace in self.workspaces.values():
            workspace_path = Path(workspace.path)
            if not workspace_path.exists():
                continue
            for task_file in task_files:
                workspace_task_path = workspace_path / task_file.name
                if workspace_task_path.exists():
                    if workspace_task_path.is_dir():
                        continue
                    workspace_task_path.unlink()
                workspace_task_path.write_text(task_file.read_text(encoding="utf-8"), encoding="utf-8")

    def _task_dir(self) -> Path | None:
        if self.task_dir is not None:
            return self.task_dir
        parents = {
            Path(workspace.path).resolve().parent
            for workspace in self.workspaces.values()
            if workspace.path
        }
        if len(parents) == 1:
            return next(iter(parents))
        return None

    async def run(
        self,
        options: RunnerOptions,
        event_callback: EventCallback | None = None,
    ) -> BenchmarkSummary:
        run_started = _now()
        run_id = options.run_id or f"{run_started.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
        run_dir = RUNS_DIR / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        self.sync_shared_task_files()

        prompt_hash = _sha256(options.prompt)
        prompt_file = run_dir / "prompt.txt"
        prompt_file.write_text(options.prompt, encoding="utf-8")

        selected = [self.workspaces[key] for key in options.selected_models]

        if options.warmup:
            for workspace in selected:
                await self._run_single(
                    run_dir=run_dir,
                    workspace=workspace,
                    prompt=options.prompt,
                    prompt_hash=prompt_hash,
                    timeout_seconds=min(60, options.timeout_seconds),
                    retries=0,
                    event_callback=None,
                    warmup=True,
                    rewrite_task_files=False,
                    rewrite_timeout_seconds=options.rewrite_timeout_seconds,
                )

        if options.mode == "sequential":
            results = []
            for workspace in selected:
                result = await self._run_single(
                    run_dir=run_dir,
                    workspace=workspace,
                    prompt=options.prompt,
                    prompt_hash=prompt_hash,
                    timeout_seconds=options.timeout_seconds,
                    retries=options.retries,
                    event_callback=event_callback,
                    warmup=False,
                    rewrite_task_files=options.rewrite_task_files,
                    rewrite_timeout_seconds=options.rewrite_timeout_seconds,
                )
                results.append(result)
        else:
            sem = asyncio.Semaphore(max(1, options.max_concurrency))
            tasks = [
                asyncio.create_task(
                    self._run_with_semaphore(
                        sem=sem,
                        run_dir=run_dir,
                        workspace=workspace,
                        prompt=options.prompt,
                        prompt_hash=prompt_hash,
                        timeout_seconds=options.timeout_seconds,
                        retries=options.retries,
                        event_callback=event_callback,
                        warmup=False,
                        rewrite_task_files=options.rewrite_task_files,
                        rewrite_timeout_seconds=options.rewrite_timeout_seconds,
                    )
                )
                for workspace in selected
            ]
            results = await asyncio.gather(*tasks)

        run_ended = _now()
        summary = BenchmarkSummary(
            run_id=run_id,
            label=options.label,
            task_dir=options.task_dir or (str(self._task_dir().name) if self._task_dir() else None),
            started_at=_iso(run_started),
            ended_at=_iso(run_ended),
            duration_ms=int((run_ended - run_started).total_seconds() * 1000),
            mode=options.mode,
            max_concurrency=options.max_concurrency,
            retries=options.retries,
            timeout_seconds=options.timeout_seconds,
            warmup=options.warmup,
            prompt_hash=prompt_hash,
            selected_models=options.selected_models,
            git_commit=_git_commit(self.root_dir),
            results=results,
        )
        summary_path = run_dir / "summary.json"
        summary_path.write_text(json.dumps(summary.to_dict(), indent=2), encoding="utf-8")
        self._write_summary_csv(run_dir, summary)
        self._write_summary_md(run_dir, summary)
        return summary

    async def _run_with_semaphore(self, sem: asyncio.Semaphore, **kwargs) -> RunJobResult:
        async with sem:
            return await self._run_single(**kwargs)

    async def _emit(self, callback: EventCallback | None, payload: dict) -> None:
        if callback:
            await callback(payload)

    async def _run_single(
        self,
        run_dir: Path,
        workspace: WorkspaceConfig,
        prompt: str,
        prompt_hash: str,
        timeout_seconds: int,
        retries: int,
        event_callback: EventCallback | None,
        warmup: bool,
        rewrite_task_files: bool = True,
        rewrite_timeout_seconds: int = 600,
    ) -> RunJobResult:
        model_dir = run_dir / workspace.key
        model_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = model_dir / ("warmup.stdout.log" if warmup else "stdout.log")
        stderr_path = model_dir / ("warmup.stderr.log" if warmup else "stderr.log")
        events_path = model_dir / ("warmup.events.jsonl" if warmup else "events.jsonl")
        result_path = model_dir / ("warmup.result.json" if warmup else "result.json")

        await self._emit(
            event_callback,
            {"type": "status", "model": workspace.key, "status": "running"},
        )

        attempts = 0
        run_started = _now()
        final_status = "failed"
        final_exit_code: int | None = None
        final_error: str | None = None
        final_stdout = ""
        final_stderr = ""
        final_events = ""
        metrics = TokenMetrics()
        task_rewrite: TaskRewriteResult | None = None

        if rewrite_task_files and not warmup:
            task_rewrite = await self._rewrite_task_files(
                workspace=workspace,
                model_dir=model_dir,
                timeout_seconds=rewrite_timeout_seconds,
                event_callback=event_callback,
            )
            if task_rewrite.status != "completed":
                run_ended = _now()
                result = RunJobResult(
                    model_key=workspace.key,
                    workspace_path=workspace.path,
                    model_name=workspace.model,
                    provider=workspace.provider,
                    status="failed",
                    exit_code=task_rewrite.exit_code,
                    started_at=_iso(run_started),
                    ended_at=_iso(run_ended),
                    duration_ms=int((run_ended - run_started).total_seconds() * 1000),
                    prompt_hash=prompt_hash,
                    stdout_path=str(stdout_path),
                    stderr_path=str(stderr_path),
                    result_path=str(result_path),
                    command=workspace.pi_args() + ["<prompt>"],
                    events_path=str(events_path),
                    task_rewrite_path=str(task_rewrite.artifact_path),
                    attempts=0,
                    error=task_rewrite.error or "Task file rewrite failed",
                    metrics=metrics,
                )
                stdout_path.write_text("", encoding="utf-8")
                stderr_path.write_text(task_rewrite.error or "", encoding="utf-8")
                events_path.write_text("", encoding="utf-8")
                result_path.write_text(json.dumps(result.to_dict(), indent=2), encoding="utf-8")
                await self._emit(
                    event_callback,
                    {
                        "type": "complete",
                        "model": workspace.key,
                        "status": result.status,
                        "exit_code": result.exit_code,
                        "duration_ms": result.duration_ms,
                        "metrics": result.metrics.__dict__,
                        "stdout_tail": "",
                        "stderr_tail": result.error or "",
                    },
                )
                return result

        command = workspace.pi_args() + [prompt]
        displayed_command = workspace.pi_args() + ["<prompt>"]
        env = load_project_env()
        exec_command = self._apply_workspace_sandbox(
            command=command,
            workspace=workspace,
            model_dir=model_dir,
        )

        while attempts <= retries:
            attempts += 1
            proc: asyncio.subprocess.Process | None = None
            stdout_task: asyncio.Task | None = None
            stderr_task: asyncio.Task | None = None
            stdout_chunks: list[str] = []
            stderr_chunks: list[str] = []
            event_chunks: list[str] = []
            attempt_metrics = TokenMetrics()
            try:
                with (
                    stdout_path.open("w", encoding="utf-8") as stdout_file,
                    stderr_path.open("w", encoding="utf-8") as stderr_file,
                    events_path.open("w", encoding="utf-8") as events_file,
                ):
                    proc = await asyncio.create_subprocess_exec(
                        *exec_command,
                        cwd=workspace.path,
                        env=env,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        limit=SUBPROCESS_STREAM_LIMIT_BYTES,
                        start_new_session=True,
                    )

                    async def drain(stream, name: str, sink: list[str], file_obj) -> None:
                        while True:
                            line = await stream.readline()
                            if not line:
                                break
                            text = line.decode("utf-8", errors="replace")
                            sink.append(text)
                            file_obj.write(text)
                            file_obj.flush()
                            await self._emit(
                                event_callback,
                                {
                                    "type": "output",
                                    "model": workspace.key,
                                    "stream": name,
                                    "text": text,
                                },
                            )

                    async def drain_pi_json_stdout(stream) -> None:
                        while True:
                            line = await stream.readline()
                            if not line:
                                break
                            text = line.decode("utf-8", errors="replace")
                            event_chunks.append(text)
                            events_file.write(text)
                            events_file.flush()
                            try:
                                event = json.loads(text)
                            except json.JSONDecodeError:
                                stdout_chunks.append(text)
                                stdout_file.write(text)
                                stdout_file.flush()
                                await self._emit(
                                    event_callback,
                                    {
                                        "type": "output",
                                        "model": workspace.key,
                                        "stream": "stdout",
                                        "text": text,
                                    },
                                )
                                continue

                            self._accumulate_pi_event_metrics(event, attempt_metrics)
                            rendered = self._render_pi_event(event)
                            if rendered:
                                stdout_chunks.append(rendered)
                                stdout_file.write(rendered)
                                stdout_file.flush()
                                await self._emit(
                                    event_callback,
                                    {
                                        "type": "output",
                                        "model": workspace.key,
                                        "stream": "stdout",
                                        "text": rendered,
                                    },
                                )

                    stdout_task = asyncio.create_task(drain_pi_json_stdout(proc.stdout))
                    stderr_task = asyncio.create_task(
                        drain(proc.stderr, "stderr", stderr_chunks, stderr_file)
                    )
                    try:
                        if timeout_seconds > 0:
                            await asyncio.wait_for(proc.wait(), timeout=timeout_seconds)
                        else:
                            await proc.wait()
                    finally:
                        if proc.returncode is None:
                            self._terminate_process_group(proc)
                            with contextlib.suppress(Exception):
                                await asyncio.wait_for(proc.wait(), timeout=10)
                        else:
                            self._terminate_process_group(proc, include_exited_leader=True)
                        await self._settle_stream_tasks([stdout_task, stderr_task])

                    final_stdout = "".join(stdout_chunks)
                    final_stderr = "".join(stderr_chunks)
                    final_events = "".join(event_chunks)
                    metrics = attempt_metrics
                    final_exit_code = proc.returncode
                    if proc.returncode == 0:
                        final_status = "completed"
                        final_error = None
                        break
                    final_error = f"Non-zero exit code: {proc.returncode}"
            except asyncio.TimeoutError:
                if proc and proc.returncode is None:
                    self._terminate_process_group(proc)
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(proc.wait(), timeout=10)
                await self._settle_stream_tasks([stdout_task, stderr_task])
                final_stdout = "".join(stdout_chunks)
                final_stderr = "".join(stderr_chunks)
                final_events = "".join(event_chunks)
                metrics = attempt_metrics
                final_exit_code = None
                final_error = f"Timed out after {timeout_seconds}s"
                final_status = "timeout"

            if attempts <= retries:
                await self._emit(
                    event_callback,
                    {
                        "type": "status",
                        "model": workspace.key,
                        "status": "retrying",
                        "attempt": attempts,
                    },
                )

        if not stdout_path.exists():
            stdout_path.write_text(final_stdout, encoding="utf-8")
        if not stderr_path.exists():
            stderr_path.write_text(final_stderr, encoding="utf-8")
        if not events_path.exists():
            events_path.write_text(final_events, encoding="utf-8")

        run_ended = _now()
        if metrics.total_tokens == 0:
            metrics = extract_inline_token_usage(final_stdout + "\n" + final_stderr) or TokenMetrics()
        result = RunJobResult(
            model_key=workspace.key,
            workspace_path=workspace.path,
            model_name=workspace.model,
            provider=workspace.provider,
            status=final_status,
            exit_code=final_exit_code,
            started_at=_iso(run_started),
            ended_at=_iso(run_ended),
            duration_ms=int((run_ended - run_started).total_seconds() * 1000),
            prompt_hash=prompt_hash,
            stdout_path=str(stdout_path),
            stderr_path=str(stderr_path),
            result_path=str(result_path),
            command=displayed_command,
            events_path=str(events_path),
            task_rewrite_path=str(task_rewrite.artifact_path) if task_rewrite else None,
            attempts=attempts,
            error=final_error,
            metrics=metrics,
        )
        result_path.write_text(json.dumps(result.to_dict(), indent=2), encoding="utf-8")

        await self._emit(
            event_callback,
            {
                "type": "complete",
                "model": workspace.key,
                "status": final_status,
                "exit_code": final_exit_code,
                "duration_ms": result.duration_ms,
                "metrics": result.metrics.__dict__,
                "stdout_tail": final_stdout[-4000:],
                "stderr_tail": final_stderr[-4000:],
            },
        )
        return result

    async def _rewrite_task_files(
        self,
        workspace: WorkspaceConfig,
        model_dir: Path,
        timeout_seconds: int,
        event_callback: EventCallback | None,
    ) -> TaskRewriteResult:
        artifact_path = model_dir / "task-rewrite.json"
        stdout_path = model_dir / "task-rewrite.stdout.log"
        stderr_path = model_dir / "task-rewrite.stderr.log"
        events_path = model_dir / "task-rewrite.events.jsonl"
        rewritten_artifact_dir = model_dir / "rewritten-task-files"
        rewrite_dir = Path(workspace.path) / ".1shot-bench-rewrites"
        rewritten_artifact_dir.mkdir(parents=True, exist_ok=True)
        rewrite_dir.mkdir(parents=True, exist_ok=True)

        task_files = discover_shared_task_files(self._task_dir())
        displayed_command = workspace.pi_args() + ["<task-rewrite-prompt>"]
        if not task_files:
            artifact = {
                "status": "completed",
                "reason": "no shared task files discovered",
                "task_files": [],
                "rewritten_files": [],
                "command": displayed_command,
            }
            artifact_path.write_text(json.dumps(artifact, indent=2), encoding="utf-8")
            stdout_path.write_text("", encoding="utf-8")
            stderr_path.write_text("", encoding="utf-8")
            events_path.write_text("", encoding="utf-8")
            return TaskRewriteResult("completed", artifact_path, stdout_path, stderr_path, events_path, displayed_command)

        await self._emit(
            event_callback,
            {"type": "status", "model": workspace.key, "status": "rewriting_task_files"},
        )

        prompt = self._task_rewrite_prompt(task_files, rewrite_dir)
        command = workspace.pi_args() + [prompt]
        exec_command = self._apply_workspace_sandbox(
            command=command,
            workspace=workspace,
            model_dir=model_dir,
        )
        proc: asyncio.subprocess.Process | None = None
        stdout = ""
        stderr = ""
        exit_code: int | None = None
        error: str | None = None
        status = "failed"
        rewritten_files: list[dict[str, str]] = []

        try:
            proc = await asyncio.create_subprocess_exec(
                *exec_command,
                cwd=workspace.path,
                env=load_project_env(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=SUBPROCESS_STREAM_LIMIT_BYTES,
                start_new_session=True,
            )
            try:
                if timeout_seconds > 0:
                    stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
                else:
                    stdout_bytes, stderr_bytes = await proc.communicate()
            except asyncio.TimeoutError:
                self._terminate_process_group(proc)
                try:
                    stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=10)
                except asyncio.TimeoutError:
                    self._kill_process_group(proc)
                    stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=10)
                error = f"Task file rewrite timed out after {timeout_seconds}s"
            stdout = stdout_bytes.decode("utf-8", errors="replace")
            stderr = stderr_bytes.decode("utf-8", errors="replace")
            exit_code = proc.returncode
            if error is None and exit_code != 0:
                error = f"Task file rewrite exited with code {exit_code}"
            if error is None:
                rewritten_files = self._install_rewritten_task_files(task_files, workspace, rewrite_dir, rewritten_artifact_dir)
                status = "completed"
        except Exception as exc:
            error = str(exc)

        stdout_path.write_text(stdout, encoding="utf-8")
        stderr_path.write_text(stderr, encoding="utf-8")
        events_path.write_text(stdout, encoding="utf-8")
        artifact = {
            "status": status,
            "error": error,
            "task_files": [str(path) for path in task_files],
            "rewrite_dir": str(rewrite_dir),
            "rewritten_files": rewritten_files,
            "command": displayed_command,
            "exit_code": exit_code,
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "events_path": str(events_path),
        }
        artifact_path.write_text(json.dumps(artifact, indent=2), encoding="utf-8")
        return TaskRewriteResult(
            status=status,
            artifact_path=artifact_path,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            events_path=events_path,
            command=displayed_command,
            exit_code=exit_code,
            error=error,
        )

    def _task_rewrite_prompt(self, task_files: list[Path], rewrite_dir: Path) -> str:
        mappings = "\n".join(
            f"- Read `./{path.name}` and write the rewritten version to `./{rewrite_dir.name}/{path.name}`."
            for path in task_files
        )
        return (
            "Before the benchmark implementation task, rewrite the task document(s) for yourself so the "
            "implementation step uses your own neutral restatement rather than the original wording.\n\n"
            "Requirements for the rewrite:\n"
            "- Preserve every requirement, acceptance criterion, constraint, file name, command, dependency, and setup instruction.\n"
            "- Keep the task scope identical; do not add features, remove features, simplify requirements, or solve the task.\n"
            "- Use your own wording and organization where helpful, but retain precise technical details.\n"
            "- Write only the rewritten task file(s) to the requested destination path(s).\n"
            "- Do not implement the product or edit any source files for the task yet.\n\n"
            f"{mappings}\n\n"
            "When all rewritten files are saved, stop."
        )

    def _install_rewritten_task_files(
        self,
        task_files: list[Path],
        workspace: WorkspaceConfig,
        rewrite_dir: Path,
        artifact_dir: Path,
    ) -> list[dict[str, str]]:
        rewritten_files: list[dict[str, str]] = []
        workspace_path = Path(workspace.path)
        for task_file in task_files:
            generated = rewrite_dir / task_file.name
            if not generated.is_file():
                raise FileNotFoundError(f"rewritten task file was not created: {generated}")
            text = generated.read_text(encoding="utf-8")
            if not text.strip():
                raise ValueError(f"rewritten task file is empty: {generated}")

            artifact_copy = artifact_dir / task_file.name
            artifact_copy.write_text(text, encoding="utf-8")

            workspace_copy = workspace_path / task_file.name
            if workspace_copy.is_symlink() or workspace_copy.exists():
                if workspace_copy.is_dir():
                    raise IsADirectoryError(f"workspace task path is a directory: {workspace_copy}")
                workspace_copy.unlink()
            workspace_copy.write_text(text, encoding="utf-8")
            rewritten_files.append(
                {
                    "source": str(task_file),
                    "workspace_path": str(workspace_copy),
                    "artifact_path": str(artifact_copy),
                }
            )
        return rewritten_files

    def _terminate_process_group(
        self,
        proc: asyncio.subprocess.Process,
        include_exited_leader: bool = False,
    ) -> None:
        if proc.pid is None:
            return
        if proc.returncode is not None and not include_exited_leader:
            return
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        except OSError:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()

    def _kill_process_group(self, proc: asyncio.subprocess.Process) -> None:
        if proc.pid is None:
            return
        if proc.returncode is not None:
            return
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        except OSError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()

    async def _settle_stream_tasks(
        self,
        tasks: list[asyncio.Task | None],
        timeout_seconds: float = 5,
    ) -> None:
        pending = [task for task in tasks if task is not None and not task.done()]
        if pending:
            _, still_pending = await asyncio.wait(pending, timeout=timeout_seconds)
            for task in still_pending:
                task.cancel()
            if still_pending:
                await asyncio.gather(*still_pending, return_exceptions=True)

        done = [task for task in tasks if task is not None and task.done()]
        if done:
            await asyncio.gather(*done, return_exceptions=True)

    def _render_pi_event(self, event: dict) -> str:
        if event.get("type") == "message_update":
            update = event.get("assistantMessageEvent") or {}
            if update.get("type") == "text_delta":
                return update.get("delta") or ""
        if event.get("type") == "tool_execution_start":
            tool_name = event.get("toolName") or "tool"
            return f"\n[tool start] {tool_name}\n"
        if event.get("type") == "tool_execution_end":
            tool_name = event.get("toolName") or "tool"
            status = "error" if event.get("isError") else "ok"
            return f"\n[tool end] {tool_name}: {status}\n"
        return ""

    def _accumulate_pi_event_metrics(self, event: dict, metrics: TokenMetrics) -> None:
        if event.get("type") != "message_end":
            return
        message = event.get("message") or {}
        if message.get("role") != "assistant":
            return
        usage = message.get("usage") or {}
        total_tokens = int(usage.get("totalTokens") or 0)
        if total_tokens <= 0:
            return
        input_tokens = int(usage.get("input") or 0)
        cache_read_tokens = int(usage.get("cacheRead") or 0)
        cache_write_tokens = int(usage.get("cacheWrite") or 0)
        metrics.input_tokens += input_tokens
        metrics.output_tokens += int(usage.get("output") or 0)
        metrics.cache_read_tokens += cache_read_tokens
        metrics.cache_write_tokens += cache_write_tokens
        metrics.prompt_tokens += input_tokens + cache_read_tokens + cache_write_tokens
        metrics.total_tokens += total_tokens
        cost = usage.get("cost") or {}
        metrics.cost_usd += float(cost.get("total") or 0)
        metrics.requests += 1

    def _apply_workspace_sandbox(
        self,
        command: list[str],
        workspace: WorkspaceConfig,
        model_dir: Path,
    ) -> list[str]:
        return apply_workspace_sandbox(
            root_dir=self.root_dir,
            workspaces=self.workspaces,
            workspace=workspace,
            model_dir=model_dir,
            command=command,
        )

    def _write_summary_csv(self, run_dir: Path, summary: BenchmarkSummary) -> None:
        csv_path = run_dir / "summary.csv"
        with csv_path.open("w", newline="", encoding="utf-8") as file_obj:
            writer = csv.writer(file_obj)
            writer.writerow(
                [
                    "model",
                    "status",
                    "duration_ms",
                    "input_tokens",
                    "output_tokens",
                    "reasoning_tokens",
                    "cache_read_tokens",
                    "cache_write_tokens",
                    "prompt_tokens",
                    "total_tokens",
                    "cost_usd",
                    "requests",
                    "exit_code",
                ]
            )
            for result in summary.results:
                m = result.metrics
                writer.writerow(
                    [
                        result.model_key,
                        result.status,
                        result.duration_ms,
                        m.input_tokens,
                        m.output_tokens,
                        m.reasoning_tokens,
                        m.cache_read_tokens,
                        m.cache_write_tokens,
                        m.prompt_tokens,
                        m.total_tokens,
                        f"{m.cost_usd:.4f}",
                        m.requests,
                        "" if result.exit_code is None else result.exit_code,
                    ]
                )

    def _write_summary_md(self, run_dir: Path, summary: BenchmarkSummary) -> None:
        md_path = run_dir / "summary.md"
        lines = [
            f"# Benchmark `{summary.run_id}`",
            "",
            f"- label: {summary.label}",
            f"- mode: {summary.mode}",
            f"- concurrency: {summary.max_concurrency}",
            f"- duration_ms: {summary.duration_ms}",
            "",
            "| model | status | duration_ms | total_tokens | cost_usd |",
            "|---|---:|---:|---:|---:|",
        ]
        for result in summary.results:
            lines.append(
                f"| {result.model_key} | {result.status} | {result.duration_ms} | "
                f"{result.metrics.total_tokens} | {result.metrics.cost_usd:.4f} |"
            )
        md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
