from __future__ import annotations

import json
import os
import stat
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from bench.deploy import (
    RenderConfig,
    deploy_run,
    detect_node_app_root,
    ensure_dockerfile,
    image_url_for,
    project_slug_for,
    redact_command,
    stage_workspace,
)


class DeployHarnessTests(unittest.TestCase):
    def test_slug_and_image_names_are_stable_and_safe(self) -> None:
        self.assertEqual(project_slug_for("frontend", "GPT++"), "1shot-bench-frontend-gpt")
        self.assertEqual(
            image_url_for("LilyJGE", "frontend", "GPT++", "run 1"),
            "ghcr.io/lilyjge/1shot-bench-frontend-gpt:run-1",
        )

    def test_detects_nested_node_app_and_skips_generated_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "task" / "gpt-workspace"
            nested = workspace / "app"
            generated = workspace / ".next"
            nested.mkdir(parents=True)
            generated.mkdir(parents=True)
            (nested / "package.json").write_text(
                json.dumps({"scripts": {"build": "next build"}, "dependencies": {"next": "16.2.6"}}),
                encoding="utf-8",
            )
            (generated / "package.json").write_text('{"scripts":{"start":"node bad.js"}}', encoding="utf-8")

            self.assertEqual(detect_node_app_root(workspace), nested)

    def test_staging_excludes_generated_files_and_preserves_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            stage = root / "stage"
            (workspace / "node_modules" / "x").mkdir(parents=True)
            (workspace / ".next").mkdir()
            (workspace / "src").mkdir()
            (workspace / "src" / "page.tsx").write_text("export default function Page() {}", encoding="utf-8")
            (workspace / "debug.log").write_text("log", encoding="utf-8")

            stage_workspace(workspace, stage)

            self.assertTrue((stage / "src" / "page.tsx").is_file())
            self.assertFalse((stage / "node_modules").exists())
            self.assertFalse((stage / ".next").exists())
            self.assertFalse((stage / "debug.log").exists())
            self.assertTrue((workspace / "node_modules" / "x").is_dir())

    def test_generates_node_and_python_dockerfiles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            node_stage = root / "node"
            node_stage.mkdir()
            (node_stage / "package.json").write_text(
                json.dumps({"scripts": {"build": "next build"}, "dependencies": {"next": "16.2.6"}}),
                encoding="utf-8",
            )
            node_shape = ensure_dockerfile(node_stage)
            self.assertEqual(node_shape.kind, "next")
            self.assertIn("next start -H 0.0.0.0", (node_stage / "Dockerfile").read_text(encoding="utf-8"))

            python_stage = root / "python"
            python_stage.mkdir()
            (python_stage / "server.py").write_text("print('ok')\n", encoding="utf-8")
            (python_stage / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
            python_shape = ensure_dockerfile(python_stage)
            self.assertEqual(python_shape.kind, "python")
            self.assertIn('CMD ["python", "server.py"]', (python_stage / "Dockerfile").read_text(encoding="utf-8"))

    def test_unsupported_workspace_writes_artifact_without_docker_or_render(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir, workspace = _write_run(root, "gpt")
            (workspace / "notes.txt").write_text("not an app", encoding="utf-8")

            results = deploy_run("run-1", root_dir=root, runs_dir=root / "runs")

            self.assertEqual(results[0].status, "unsupported")
            artifact = json.loads((run_dir / "gpt" / "deployment.json").read_text(encoding="utf-8"))
            self.assertEqual(artifact["status"], "unsupported")
            self.assertIn("no Dockerfile", artifact["reason"])

    def test_redacts_ghcr_token_and_render_hook(self) -> None:
        config = RenderConfig(
            ghcr_username="user",
            ghcr_token="secret-token",
            ghcr_owner="owner",
            deploy_hooks={("TASK", "GPT"): "https://api.render.com/deploy/srv-secret?key=value"},
            service_urls={},
        )
        redacted = redact_command(
            ["docker", "login", "--password", "secret-token", "https://api.render.com/deploy/srv-secret?key=value"],
            config,
        )
        self.assertNotIn("secret-token", json.dumps(redacted))
        self.assertNotIn("srv-secret", json.dumps(redacted))

    def test_fake_docker_and_hook_deploy_records_image_and_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir, workspace = _write_run(root, "gpt")
            _write_next_app(workspace)
            bin_dir = root / "bin"
            calls_path = root / "docker-calls.jsonl"
            _write_fake_docker(bin_dir, calls_path)
            server = _HookServer()
            server.start()

            old_env = _set_env(
                {
                    "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
                    "GHCR_USERNAME": "gh-user",
                    "GHCR_TOKEN": "secret-token",
                    "GHCR_OWNER": "LilyJGE",
                    "RENDER_DEPLOY_HOOK_TASK_GPT": server.url,
                    "RENDER_SERVICE_URL_TASK_GPT": "https://pi-bench-task-gpt.onrender.com",
                }
            )
            try:
                results = deploy_run("run-1", root_dir=root, runs_dir=root / "runs")
            finally:
                _restore_env(old_env)
                server.stop()

            self.assertEqual(results[0].status, "completed")
            self.assertEqual(results[0].preview_url, "https://pi-bench-task-gpt.onrender.com")
            docker_calls = [json.loads(line) for line in calls_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([call["argv"][1] for call in docker_calls], ["build", "login", "push"])
            self.assertIn("--platform", docker_calls[0]["argv"])
            self.assertIn("linux/amd64", docker_calls[0]["argv"])
            self.assertEqual(server.query["imgURL"][0], "ghcr.io/lilyjge/1shot-bench-task-gpt:run-1")
            artifact = json.loads((run_dir / "gpt" / "deployment.json").read_text(encoding="utf-8"))
            self.assertEqual(artifact["image_url"], "ghcr.io/lilyjge/1shot-bench-task-gpt:run-1")
            self.assertNotIn("secret-token", json.dumps(artifact))
            self.assertNotIn("/deploy/", artifact["deploy_hook_url"].removesuffix("/deploy/<redacted-render-hook>"))
            self.assertFalse((run_dir / "deploy-staging" / "1shot-bench-task-gpt" / "node_modules").exists())

    def test_failed_docker_build_records_failure_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir, workspace = _write_run(root, "gpt")
            _write_next_app(workspace)
            bin_dir = root / "bin"
            _write_fake_docker(bin_dir, root / "docker-calls.jsonl", fail_build=True)
            old_env = _set_env(
                {
                    "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
                    "GHCR_USERNAME": "gh-user",
                    "GHCR_TOKEN": "secret-token",
                    "GHCR_OWNER": "LilyJGE",
                    "RENDER_DEPLOY_HOOK_TASK_GPT": "http://127.0.0.1:1/deploy/srv-secret",
                }
            )
            try:
                results = deploy_run("run-1", root_dir=root, runs_dir=root / "runs")
            finally:
                _restore_env(old_env)

            self.assertEqual(results[0].status, "failed")
            artifact = json.loads((run_dir / "gpt" / "deployment.json").read_text(encoding="utf-8"))
            self.assertIn("docker build exited", artifact["reason"])
            self.assertTrue((run_dir / "gpt" / "deployment.stderr.log").is_file())

    def test_timed_out_docker_build_records_failure_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir, workspace = _write_run(root, "gpt")
            _write_next_app(workspace)
            bin_dir = root / "bin"
            _write_fake_docker(bin_dir, root / "docker-calls.jsonl", sleep_build=True)
            old_env = _set_env(
                {
                    "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
                    "GHCR_USERNAME": "gh-user",
                    "GHCR_TOKEN": "secret-token",
                    "GHCR_OWNER": "LilyJGE",
                    "RENDER_DEPLOY_HOOK_TASK_GPT": "http://127.0.0.1:1/deploy/srv-secret",
                }
            )
            try:
                results = deploy_run(
                    "run-1",
                    root_dir=root,
                    runs_dir=root / "runs",
                    docker_timeout_seconds=1,
                )
            finally:
                _restore_env(old_env)

            self.assertEqual(results[0].status, "failed")
            artifact = json.loads((run_dir / "gpt" / "deployment.json").read_text(encoding="utf-8"))
            self.assertIn("docker build timed out", artifact["reason"])
            self.assertIn("timed out after", (run_dir / "gpt" / "deployment.stderr.log").read_text(encoding="utf-8"))

    def test_failed_render_hook_records_failure_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir, workspace = _write_run(root, "gpt")
            _write_next_app(workspace)
            bin_dir = root / "bin"
            _write_fake_docker(bin_dir, root / "docker-calls.jsonl")
            server = _HookServer(status=500)
            server.start()
            old_env = _set_env(
                {
                    "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
                    "GHCR_USERNAME": "gh-user",
                    "GHCR_TOKEN": "secret-token",
                    "GHCR_OWNER": "LilyJGE",
                    "RENDER_DEPLOY_HOOK_TASK_GPT": server.url,
                }
            )
            try:
                results = deploy_run("run-1", root_dir=root, runs_dir=root / "runs")
            finally:
                _restore_env(old_env)
                server.stop()

            self.assertEqual(results[0].status, "failed")
            artifact = json.loads((run_dir / "gpt" / "deployment.json").read_text(encoding="utf-8"))
            self.assertIn("Render deploy hook", artifact["reason"])


class _HookHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        self.server.query = parse_qs(urlsplit(self.path).query)  # type: ignore[attr-defined]
        self.send_response(self.server.status)  # type: ignore[attr-defined]
        self.end_headers()
        self.wfile.write(b'{"deploy":"ok"}')

    def log_message(self, format: str, *args) -> None:
        return


class _HookServer:
    def __init__(self, status: int = 202):
        self.status = status
        self.httpd: HTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.query: dict[str, list[str]] = {}
        self.url = ""

    def start(self) -> None:
        self.httpd = HTTPServer(("127.0.0.1", 0), _HookHandler)
        self.httpd.status = self.status  # type: ignore[attr-defined]
        self.httpd.query = self.query  # type: ignore[attr-defined]
        port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{port}/deploy/srv-secret"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        if self.httpd:
            self.query = self.httpd.query  # type: ignore[attr-defined]
            self.httpd.shutdown()
            self.httpd.server_close()
        if self.thread:
            self.thread.join(timeout=2)


def _write_run(root: Path, model: str) -> tuple[Path, Path]:
    workspace = root / "task" / f"{model}-workspace"
    workspace.mkdir(parents=True)
    run_dir = root / "runs" / "run-1"
    (run_dir / model).mkdir(parents=True)
    summary = {"run_id": "run-1", "results": [{"model_key": model, "workspace_path": str(workspace)}]}
    (run_dir / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    return run_dir, workspace


def _write_next_app(workspace: Path) -> None:
    (workspace / "node_modules" / "x").mkdir(parents=True)
    (workspace / ".next").mkdir()
    (workspace / "package.json").write_text(
        json.dumps({"scripts": {"build": "next build"}, "dependencies": {"next": "16.2.6"}}),
        encoding="utf-8",
    )
    (workspace / "page.log").write_text("log", encoding="utf-8")


def _write_fake_docker(
    bin_dir: Path,
    calls_path: Path,
    *,
    fail_build: bool = False,
    sleep_build: bool = False,
) -> None:
    bin_dir.mkdir()
    fake = bin_dir / "docker"
    fake.write_text(
        "\n".join(
            [
                "#!/usr/bin/env python3",
                "import json, sys, time",
                f"calls_path = {str(calls_path)!r}",
                "with open(calls_path, 'a', encoding='utf-8') as f:",
                "    f.write(json.dumps({'argv': sys.argv}) + '\\n')",
                "if len(sys.argv) > 1 and sys.argv[1] == 'build':",
                f"    time.sleep(5) if {sleep_build!r} else None",
                f"    sys.exit(42) if {fail_build!r} else print('built')",
                "elif len(sys.argv) > 1 and sys.argv[1] == 'login':",
                "    print('logged in')",
                "elif len(sys.argv) > 1 and sys.argv[1] == 'push':",
                "    print('pushed')",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)


def _set_env(values: dict[str, str]) -> dict[str, str | None]:
    old = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    return old


def _restore_env(values: dict[str, str | None]) -> None:
    for key, value in values.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


if __name__ == "__main__":
    unittest.main()
