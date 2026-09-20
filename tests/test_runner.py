from __future__ import annotations

import contextlib
import json
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from bench.runner import BenchmarkRunner, RunnerOptions
from bench.schemas import WorkspaceConfig


class RunnerLoggingTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_rewrites_task_file_before_implementation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bin_dir = root / "bin"
            task_dir = root / "task"
            workspace_dir = task_dir / "fake-workspace"
            runs_dir = root / "runs"
            bin_dir.mkdir()
            workspace_dir.mkdir(parents=True)
            runs_dir.mkdir()
            (task_dir / "PRD.md").write_text("original prd\n", encoding="utf-8")

            fake_pi = bin_dir / "pi"
            fake_pi.write_text(
                "\n".join(
                    [
                        "#!/usr/bin/env python3",
                        "import json, pathlib, sys",
                        "prompt = sys.argv[-1]",
                        "if 'Before the benchmark implementation task' in prompt:",
                        "    pathlib.Path('.1shot-bench-rewrites/PRD.md').write_text('rewritten prd\\n', encoding='utf-8')",
                        "    delta = 'rewrite complete\\n'",
                        "else:",
                        "    pathlib.Path('implementation-prompt.txt').write_text(prompt, encoding='utf-8')",
                        "    delta = 'implementation complete\\n'",
                        "event = {",
                        "    'type': 'message_update',",
                        "    'assistantMessageEvent': {'type': 'text_delta', 'delta': delta},",
                        "}",
                        "print(json.dumps(event), flush=True)",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            fake_pi.chmod(fake_pi.stat().st_mode | stat.S_IXUSR)

            old_path = os.environ.get("PATH", "")
            os.environ["PATH"] = f"{bin_dir}{os.pathsep}{old_path}"
            try:
                workspace = WorkspaceConfig(
                    key="fake",
                    name="Fake",
                    path=str(workspace_dir),
                    model="fake-model",
                    provider="fake-provider",
                )
                runner = BenchmarkRunner(root, {"fake": workspace})
                runner._apply_workspace_sandbox = lambda command, workspace, model_dir: command
                options = RunnerOptions(
                    prompt="Please read PRD.md and complete the task.",
                    selected_models=["fake"],
                    mode="sequential",
                    run_id="rewrite-test",
                )
                with mock.patch("bench.runner.RUNS_DIR", runs_dir):
                    summary = await runner.run(options)
            finally:
                os.environ["PATH"] = old_path

            self.assertEqual(summary.results[0].status, "completed")
            self.assertFalse((workspace_dir / "PRD.md").is_symlink())
            self.assertEqual((workspace_dir / "PRD.md").read_text(encoding="utf-8"), "rewritten prd\n")
            self.assertEqual(
                (workspace_dir / "implementation-prompt.txt").read_text(encoding="utf-8"),
                "Please read PRD.md and complete the task.",
            )
            rewrite_path = Path(summary.results[0].task_rewrite_path or "")
            self.assertTrue(rewrite_path.is_file())
            rewrite_artifact = json.loads(rewrite_path.read_text(encoding="utf-8"))
            self.assertEqual(rewrite_artifact["status"], "completed")
            self.assertEqual(
                (runs_dir / "rewrite-test" / "fake" / "rewritten-task-files" / "PRD.md").read_text(encoding="utf-8"),
                "rewritten prd\n",
            )
            runner.sync_shared_task_files()
            self.assertFalse((workspace_dir / "PRD.md").is_symlink())
            self.assertEqual((workspace_dir / "PRD.md").read_text(encoding="utf-8"), "original prd\n")

    async def test_timeout_preserves_live_stdout_and_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bin_dir = root / "bin"
            workspace_dir = root / "workspace"
            run_dir = root / "runs" / "live-timeout"
            bin_dir.mkdir()
            workspace_dir.mkdir()
            run_dir.mkdir(parents=True)

            fake_pi = bin_dir / "pi"
            fake_pi.write_text(
                "\n".join(
                    [
                        "#!/usr/bin/env python3",
                        "import json, time",
                        "event = {",
                        "    'type': 'message_update',",
                        "    'assistantMessageEvent': {'type': 'text_delta', 'delta': 'live-log\\n'},",
                        "}",
                        "print(json.dumps(event), flush=True)",
                        "time.sleep(60)",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            fake_pi.chmod(fake_pi.stat().st_mode | stat.S_IXUSR)

            old_path = os.environ.get("PATH", "")
            os.environ["PATH"] = f"{bin_dir}{os.pathsep}{old_path}"
            try:
                workspace = WorkspaceConfig(
                    key="fake",
                    name="Fake",
                    path=str(workspace_dir),
                    model="fake-model",
                    provider="fake-provider",
                )
                runner = BenchmarkRunner(root, {"fake": workspace})
                runner._apply_workspace_sandbox = lambda command, workspace, model_dir: command

                result = await runner._run_single(
                    run_dir=run_dir,
                    workspace=workspace,
                    prompt="hello",
                    prompt_hash="hash",
                    timeout_seconds=1,
                    retries=0,
                    event_callback=None,
                    warmup=False,
                )
            finally:
                os.environ["PATH"] = old_path

            self.assertEqual(result.status, "timeout")
            self.assertIn("live-log", (run_dir / "fake" / "stdout.log").read_text())
            self.assertIn("message_update", (run_dir / "fake" / "events.jsonl").read_text())

    async def test_completed_process_cleans_lingering_process_group_children(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bin_dir = root / "bin"
            workspace_dir = root / "workspace"
            run_dir = root / "runs" / "group-cleanup"
            child_pid_file = root / "child.pid"
            bin_dir.mkdir()
            workspace_dir.mkdir()
            run_dir.mkdir(parents=True)

            fake_pi = bin_dir / "pi"
            fake_pi.write_text(
                "\n".join(
                    [
                        "#!/usr/bin/env python3",
                        "import json, os, subprocess",
                        "child = subprocess.Popen(",
                        "    ['sleep', '60'],",
                        "    stdin=subprocess.DEVNULL,",
                        "    stdout=subprocess.DEVNULL,",
                        "    stderr=subprocess.DEVNULL,",
                        ")",
                        "with open(os.environ['CHILD_PID_FILE'], 'w') as f:",
                        "    f.write(str(child.pid))",
                        "event = {",
                        "    'type': 'message_update',",
                        "    'assistantMessageEvent': {'type': 'text_delta', 'delta': 'done\\n'},",
                        "}",
                        "print(json.dumps(event), flush=True)",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            fake_pi.chmod(fake_pi.stat().st_mode | stat.S_IXUSR)

            old_path = os.environ.get("PATH", "")
            os.environ["PATH"] = f"{bin_dir}{os.pathsep}{old_path}"
            os.environ["CHILD_PID_FILE"] = str(child_pid_file)
            try:
                workspace = WorkspaceConfig(
                    key="fake",
                    name="Fake",
                    path=str(workspace_dir),
                    model="fake-model",
                    provider="fake-provider",
                )
                runner = BenchmarkRunner(root, {"fake": workspace})
                runner._apply_workspace_sandbox = lambda command, workspace, model_dir: command

                result = await runner._run_single(
                    run_dir=run_dir,
                    workspace=workspace,
                    prompt="hello",
                    prompt_hash="hash",
                    timeout_seconds=10,
                    retries=0,
                    event_callback=None,
                    warmup=False,
                )
            finally:
                os.environ["PATH"] = old_path
                os.environ.pop("CHILD_PID_FILE", None)

            self.assertEqual(result.status, "completed")
            child_pid = int(child_pid_file.read_text(encoding="utf-8"))
            for _ in range(20):
                if not _pid_is_alive(child_pid):
                    break
                time.sleep(0.1)
            self.assertFalse(_pid_is_alive(child_pid))


def _pid_is_alive(pid: int) -> bool:
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, 0)
        return True
    return False


if __name__ == "__main__":
    unittest.main()
