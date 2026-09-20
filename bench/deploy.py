from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from bench.config import ROOT_DIR, RUNS_DIR


DEPLOYMENT_PROVIDER = "render"
DEFAULT_DOCKER_TIMEOUT_SECONDS = 1800
EXCLUDED_STAGE_NAMES = {
    ".cache",
    ".git",
    ".next",
    ".pytest_cache",
    ".turbo",
    ".vercel",
    "__pycache__",
    "build",
    "coverage",
    "dist",
    "node_modules",
    "playwright-report",
    "test-results",
    "runs",
}
EXCLUDED_STAGE_SUFFIXES = (".log", ".pid")


@dataclass
class RenderConfig:
    ghcr_username: str
    ghcr_token: str
    ghcr_owner: str
    deploy_hooks: dict[tuple[str, str], str]
    service_urls: dict[tuple[str, str], str]
    default_plan_note: str | None = None


@dataclass
class AppShape:
    kind: str
    app_root: Path
    dockerfile_generated: bool


@dataclass
class DeploymentResult:
    model_key: str
    provider: str
    task_dir: str
    source_workspace: str
    staged_path: str
    image_url: str
    status: str
    preview_url: str | None = None
    reason: str | None = None
    app_root: str | None = None
    dockerfile_generated: bool = False
    build_command: list[str] | None = None
    login_command: list[str] | None = None
    push_command: list[str] | None = None
    deploy_hook_url: str | None = None
    deploy_hook_status: int | None = None
    stdout_path: str | None = None
    stderr_path: str | None = None
    artifact_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def deploy_run(
    run_id: str,
    *,
    provider: str = DEPLOYMENT_PROVIDER,
    root_dir: Path = ROOT_DIR,
    runs_dir: Path = RUNS_DIR,
    docker_timeout_seconds: int = DEFAULT_DOCKER_TIMEOUT_SECONDS,
) -> list[DeploymentResult]:
    if provider != DEPLOYMENT_PROVIDER:
        raise ValueError(f"Unsupported deployment provider: {provider}")
    run_dir = runs_dir / run_id
    summary_path = run_dir / "summary.json"
    if not summary_path.exists():
        raise FileNotFoundError(f"Run summary not found: {summary_path}")
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    config = load_render_config(root_dir)
    results: list[DeploymentResult] = []
    for result in summary.get("results", []):
        deployment = deploy_model_result(
            run_dir=run_dir,
            result=result,
            config=config,
            root_dir=root_dir,
            docker_timeout_seconds=docker_timeout_seconds,
        )
        results.append(deployment)
    (run_dir / "deployments.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "provider": provider,
                "deployments": [result.to_dict() for result in results],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return results


def deploy_model_result(
    *,
    run_dir: Path,
    result: dict[str, Any],
    config: RenderConfig | None,
    root_dir: Path = ROOT_DIR,
    docker_timeout_seconds: int = DEFAULT_DOCKER_TIMEOUT_SECONDS,
) -> DeploymentResult:
    model_key = str(result.get("model_key") or "unknown")
    workspace = Path(str(result.get("workspace_path") or ""))
    task_dir = workspace.parent.name if workspace.parent.name else "task"
    slug = project_slug_for(task_dir, model_key)
    image_url = image_url_for(config.ghcr_owner if config else "missing-owner", task_dir, model_key, run_dir.name)
    model_dir = run_dir / model_key
    model_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = model_dir / "deployment.json"
    stdout_path = model_dir / "deployment.stdout.log"
    stderr_path = model_dir / "deployment.stderr.log"
    stage_dir = run_dir / "deploy-staging" / slug

    deployment = DeploymentResult(
        model_key=model_key,
        provider=DEPLOYMENT_PROVIDER,
        task_dir=task_dir,
        source_workspace=str(workspace),
        staged_path=str(stage_dir),
        image_url=image_url,
        status="unsupported",
        stdout_path=str(stdout_path),
        stderr_path=str(stderr_path),
        artifact_path=str(artifact_path),
    )

    if not workspace.exists():
        deployment.reason = f"workspace not found: {workspace}"
        _write_deployment_artifacts(deployment, artifact_path, stdout_path, stderr_path)
        return deployment

    stage_workspace(workspace, stage_dir)
    try:
        shape = ensure_dockerfile(stage_dir)
    except ValueError as exc:
        deployment.reason = str(exc)
        _write_deployment_artifacts(deployment, artifact_path, stdout_path, stderr_path)
        return deployment
    deployment.app_root = str(shape.app_root)
    deployment.dockerfile_generated = shape.dockerfile_generated

    if not config:
        deployment.status = "skipped"
        deployment.reason = "GHCR/Render configuration is missing from environment or .codex-private/render.env"
        _write_deployment_artifacts(deployment, artifact_path, stdout_path, stderr_path)
        return deployment
    image_url = image_url_for(config.ghcr_owner, task_dir, model_key, run_dir.name)
    deployment.image_url = image_url
    hook = deploy_hook_for(config, task_dir, model_key)
    if not hook:
        deployment.status = "skipped"
        deployment.reason = f"Render deploy hook is not configured for {task_dir}/{model_key}"
        _write_deployment_artifacts(deployment, artifact_path, stdout_path, stderr_path)
        return deployment
    deployment.preview_url = config.service_urls.get((task_key(task_dir), model_key.upper()))
    deployment.deploy_hook_url = redact_hook_url(hook)

    build_command = ["docker", "build", "--platform", "linux/amd64", "-t", image_url, str(stage_dir)]
    login_command = ["docker", "login", "ghcr.io", "-u", config.ghcr_username, "--password-stdin"]
    push_command = ["docker", "push", image_url]
    deployment.build_command = redact_command(build_command, config)
    deployment.login_command = redact_command(login_command, config)
    deployment.push_command = redact_command(push_command, config)

    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    try:
        build_proc = _run_command(build_command, cwd=root_dir, config=config, timeout_seconds=docker_timeout_seconds)
    except FileNotFoundError:
        deployment.status = "failed"
        deployment.reason = "`docker` command is not available in PATH"
        _write_completed_deployment(deployment, artifact_path, stdout_path, stderr_path, stdout_chunks, stderr_chunks)
        return deployment
    except subprocess.TimeoutExpired as exc:
        deployment.status = "failed"
        deployment.reason = f"docker build timed out after {docker_timeout_seconds}s"
        _append_proc_output(stdout_chunks, stderr_chunks, deployment.build_command, _completed_from_timeout(exc, config))
        _write_completed_deployment(deployment, artifact_path, stdout_path, stderr_path, stdout_chunks, stderr_chunks)
        return deployment
    _append_proc_output(stdout_chunks, stderr_chunks, deployment.build_command, build_proc)
    if build_proc.returncode != 0:
        deployment.status = "failed"
        deployment.reason = f"docker build exited with code {build_proc.returncode}"
        _write_completed_deployment(deployment, artifact_path, stdout_path, stderr_path, stdout_chunks, stderr_chunks)
        return deployment

    try:
        login_proc = _run_command(
            login_command,
            cwd=root_dir,
            config=config,
            input_text=f"{config.ghcr_token}\n",
            timeout_seconds=docker_timeout_seconds,
        )
    except FileNotFoundError:
        deployment.status = "failed"
        deployment.reason = "`docker` command is not available in PATH"
        _write_completed_deployment(deployment, artifact_path, stdout_path, stderr_path, stdout_chunks, stderr_chunks)
        return deployment
    except subprocess.TimeoutExpired as exc:
        deployment.status = "failed"
        deployment.reason = f"docker login timed out after {docker_timeout_seconds}s"
        _append_proc_output(stdout_chunks, stderr_chunks, deployment.login_command, _completed_from_timeout(exc, config))
        _write_completed_deployment(deployment, artifact_path, stdout_path, stderr_path, stdout_chunks, stderr_chunks)
        return deployment
    _append_proc_output(stdout_chunks, stderr_chunks, deployment.login_command, login_proc)
    if login_proc.returncode != 0:
        deployment.status = "failed"
        deployment.reason = f"docker login exited with code {login_proc.returncode}"
        _write_completed_deployment(deployment, artifact_path, stdout_path, stderr_path, stdout_chunks, stderr_chunks)
        return deployment

    try:
        push_proc = _run_command(push_command, cwd=root_dir, config=config, timeout_seconds=docker_timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        deployment.status = "failed"
        deployment.reason = f"docker push timed out after {docker_timeout_seconds}s"
        _append_proc_output(stdout_chunks, stderr_chunks, deployment.push_command, _completed_from_timeout(exc, config))
        _write_completed_deployment(deployment, artifact_path, stdout_path, stderr_path, stdout_chunks, stderr_chunks)
        return deployment
    _append_proc_output(stdout_chunks, stderr_chunks, deployment.push_command, push_proc)
    if push_proc.returncode != 0:
        deployment.status = "failed"
        deployment.reason = f"docker push exited with code {push_proc.returncode}"
        _write_completed_deployment(deployment, artifact_path, stdout_path, stderr_path, stdout_chunks, stderr_chunks)
        return deployment

    hook_result = trigger_render_deploy_hook(hook, image_url, config=config)
    deployment.deploy_hook_status = hook_result.returncode
    stdout_chunks.append(f"$ POST {deployment.deploy_hook_url}?imgURL=<encoded-image-url>\n{hook_result.stdout or ''}")
    stderr_chunks.append(hook_result.stderr or "")
    if hook_result.returncode not in {200, 201, 202, 204}:
        deployment.status = "failed"
        deployment.reason = f"Render deploy hook returned HTTP {hook_result.returncode}"
    else:
        deployment.status = "completed"

    _write_completed_deployment(deployment, artifact_path, stdout_path, stderr_path, stdout_chunks, stderr_chunks)
    return deployment


def load_render_config(root_dir: Path = ROOT_DIR) -> RenderConfig | None:
    env = _read_private_render_env(root_dir)
    merged = dict(env)
    merged.update(os.environ)
    username = merged.get("GHCR_USERNAME")
    token = merged.get("GHCR_TOKEN")
    owner = merged.get("GHCR_OWNER")
    if not username or not token or not owner:
        return None
    hooks: dict[tuple[str, str], str] = {}
    service_urls: dict[tuple[str, str], str] = {}
    for key, value in merged.items():
        if key.startswith("RENDER_DEPLOY_HOOK_") and value:
            suffix = key.removeprefix("RENDER_DEPLOY_HOOK_")
            parts = suffix.rsplit("_", 1)
            if len(parts) == 2:
                hooks[(parts[0], parts[1])] = value
        if key.startswith("RENDER_SERVICE_URL_") and value:
            suffix = key.removeprefix("RENDER_SERVICE_URL_")
            parts = suffix.rsplit("_", 1)
            if len(parts) == 2:
                service_urls[(parts[0], parts[1])] = value
    return RenderConfig(
        ghcr_username=username,
        ghcr_token=token,
        ghcr_owner=owner,
        deploy_hooks=hooks,
        service_urls=service_urls,
        default_plan_note=merged.get("RENDER_DEFAULT_PLAN_NOTE") or None,
    )


def deploy_hook_for(config: RenderConfig, task_dir: str, model_key: str) -> str | None:
    return config.deploy_hooks.get((task_key(task_dir), model_key.upper()))


def project_slug_for(task_dir: str, model_key: str) -> str:
    raw = f"1shot-bench-{task_dir}-{model_key}".lower()
    slug = re.sub(r"[^a-z0-9._-]+", "-", raw)
    slug = re.sub(r"-{2,}", "-", slug).strip(".-_")
    return slug[:100].strip(".-_") or "1shot-bench-demo"


def image_url_for(owner: str, task_dir: str, model_key: str, run_id: str) -> str:
    owner_slug = owner.strip().lower()
    image_name = project_slug_for(task_dir, model_key)
    tag = re.sub(r"[^a-zA-Z0-9._-]+", "-", run_id).strip(".-_") or "latest"
    return f"ghcr.io/{owner_slug}/{image_name}:{tag}"


def task_key(task_dir: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", task_dir.upper()).strip("_")


def stage_workspace(workspace: Path, stage_dir: Path) -> None:
    if stage_dir.exists():
        shutil.rmtree(stage_dir)
    shutil.copytree(workspace, stage_dir, ignore=_stage_ignore)


def ensure_dockerfile(stage_dir: Path) -> AppShape:
    dockerfile = stage_dir / "Dockerfile"
    if dockerfile.exists():
        return AppShape("custom", stage_dir, False)
    node_root = detect_node_app_root(stage_dir)
    if node_root:
        dockerfile.write_text(_node_dockerfile(node_root.relative_to(stage_dir), _node_app_kind(node_root)), encoding="utf-8")
        return AppShape(_node_app_kind(node_root), node_root, True)
    python_root = detect_python_app_root(stage_dir)
    if python_root:
        dockerfile.write_text(_python_dockerfile(python_root.relative_to(stage_dir)), encoding="utf-8")
        return AppShape("python", python_root, True)
    raise ValueError("no Dockerfile or runnable Node/Python web app detected")


def detect_node_app_root(stage_dir: Path) -> Path | None:
    candidates: list[Path] = []
    for package_json in sorted(stage_dir.glob("**/package.json")):
        if _excluded_path(package_json):
            continue
        try:
            package = json.loads(package_json.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        scripts = package.get("scripts") or {}
        deps = {**(package.get("dependencies") or {}), **(package.get("devDependencies") or {})}
        parent = package_json.parent
        if "next" in deps or "start" in scripts or "build" in scripts:
            candidates.append(parent)
    if not candidates:
        return None
    candidates.sort(key=lambda path: (len(path.relative_to(stage_dir).parts), str(path)))
    return candidates[0]


def detect_python_app_root(stage_dir: Path) -> Path | None:
    candidates: list[Path] = []
    for server in sorted(stage_dir.glob("**/server.py")):
        if _excluded_path(server):
            continue
        parent = server.parent
        if (parent / "requirements.txt").exists() or (parent / "pyproject.toml").exists() or server.exists():
            candidates.append(parent)
    if not candidates:
        return None
    candidates.sort(key=lambda path: (len(path.relative_to(stage_dir).parts), str(path)))
    return candidates[0]


def redact_command(command: list[str], config: RenderConfig) -> list[str]:
    return [redact_text(part, config) for part in command]


def redact_text(value: str, config: RenderConfig) -> str:
    redacted = value.replace(config.ghcr_token, "<redacted-ghcr-token>")
    for hook in config.deploy_hooks.values():
        redacted = redacted.replace(hook, redact_hook_url(hook))
    return redacted


def redact_hook_url(url: str) -> str:
    parsed = urlsplit(url)
    if not parsed.netloc:
        return "<redacted-render-deploy-hook>"
    return urlunsplit((parsed.scheme, parsed.netloc, "/deploy/<redacted-render-hook>", "", ""))


def trigger_render_deploy_hook(hook_url: str, image_url: str, *, config: RenderConfig) -> subprocess.CompletedProcess[str]:
    separator = "&" if "?" in hook_url else "?"
    url = f"{hook_url}{separator}imgURL={quote(image_url, safe='')}"
    request = Request(url, method="POST")
    try:
        with urlopen(request, timeout=60) as response:
            body = response.read().decode("utf-8", errors="replace")
            return subprocess.CompletedProcess(["render-deploy-hook"], response.status, redact_text(body, config), "")
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        stderr = body or str(exc)
        return subprocess.CompletedProcess(["render-deploy-hook"], exc.code, "", redact_text(stderr, config))
    except Exception as exc:
        return subprocess.CompletedProcess(["render-deploy-hook"], 0, "", redact_text(str(exc), config))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Deploy 1ShotBench run outputs")
    parser.add_argument("--run-id", required=True, help="Run id under runs/")
    parser.add_argument("--provider", choices=[DEPLOYMENT_PROVIDER], default=DEPLOYMENT_PROVIDER)
    parser.add_argument(
        "--docker-timeout-seconds",
        type=int,
        default=DEFAULT_DOCKER_TIMEOUT_SECONDS,
        help="Timeout for each docker build/login/push command.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    results = deploy_run(args.run_id, provider=args.provider, docker_timeout_seconds=args.docker_timeout_seconds)
    print(json.dumps([result.to_dict() for result in results], indent=2))
    return 0


def _read_private_render_env(root_dir: Path) -> dict[str, str]:
    path = root_dir / ".codex-private" / "render.env"
    if not path.exists():
        return {}
    env: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def _excluded_path(path: Path) -> bool:
    scan_excluded = EXCLUDED_STAGE_NAMES - {"runs"}
    return any(part in scan_excluded for part in path.parts)


def _stage_ignore(dir_path: str, names: list[str]) -> set[str]:
    return {
        name
        for name in names
        if name in EXCLUDED_STAGE_NAMES or name.endswith(EXCLUDED_STAGE_SUFFIXES)
    }


def _node_app_kind(app_root: Path) -> str:
    package = json.loads((app_root / "package.json").read_text(encoding="utf-8"))
    deps = {**(package.get("dependencies") or {}), **(package.get("devDependencies") or {})}
    return "next" if "next" in deps else "node"


def _node_dockerfile(app_rel: Path, kind: str) -> str:
    workdir = "/app" if str(app_rel) == "." else f"/app/{app_rel.as_posix()}"
    start = 'CMD ["sh", "-c", "npx next start -H 0.0.0.0 -p ${PORT:-10000}"]' if kind == "next" else 'CMD ["npm", "start"]'
    return "\n".join(
        [
            "FROM node:22-bookworm-slim",
            "ENV NODE_ENV=production",
            "WORKDIR /app",
            "COPY . .",
            f"WORKDIR {workdir}",
            "RUN if [ -f package-lock.json ]; then npm ci; else npm install; fi",
            "RUN if npm run | grep -q ' build'; then npm run build; fi",
            "EXPOSE 10000",
            start,
            "",
        ]
    )


def _python_dockerfile(app_rel: Path) -> str:
    workdir = "/app" if str(app_rel) == "." else f"/app/{app_rel.as_posix()}"
    return "\n".join(
        [
            "FROM python:3.12-slim",
            "ENV PYTHONUNBUFFERED=1",
            "ENV HOST=0.0.0.0",
            "ENV PORT=10000",
            "WORKDIR /app",
            "COPY . .",
            f"WORKDIR {workdir}",
            "RUN if [ -f requirements.txt ]; then pip install --no-cache-dir -r requirements.txt; fi",
            "EXPOSE 10000",
            'CMD ["python", "server.py"]',
            "",
        ]
    )


def _run_command(
    command: list[str],
    *,
    cwd: Path,
    config: RenderConfig,
    input_text: str | None = None,
    timeout_seconds: int = DEFAULT_DOCKER_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        command,
        cwd=str(cwd),
        input=input_text,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )
    return subprocess.CompletedProcess(
        proc.args,
        proc.returncode,
        redact_text(proc.stdout or "", config),
        redact_text(proc.stderr or "", config),
    )


def _completed_from_timeout(
    exc: subprocess.TimeoutExpired,
    config: RenderConfig,
) -> subprocess.CompletedProcess[str]:
    stdout = _timeout_output_to_text(exc.stdout)
    stderr = _timeout_output_to_text(exc.stderr)
    if stderr:
        stderr = f"{stderr}\n"
    stderr += f"timed out after {exc.timeout}s"
    return subprocess.CompletedProcess(
        exc.cmd,
        -1,
        redact_text(stdout, config),
        redact_text(stderr, config),
    )


def _timeout_output_to_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _append_proc_output(
    stdout_chunks: list[str],
    stderr_chunks: list[str],
    redacted_command: list[str] | None,
    proc: subprocess.CompletedProcess[str],
) -> None:
    stdout_chunks.append(f"$ {' '.join(redacted_command or [])}\n{proc.stdout or ''}")
    stderr_chunks.append(f"$ {' '.join(redacted_command or [])}\n{proc.stderr or ''}")


def _write_deployment_artifacts(
    result: DeploymentResult,
    artifact_path: Path,
    stdout_path: Path,
    stderr_path: Path,
) -> None:
    stdout_path.write_text("", encoding="utf-8")
    stderr_path.write_text(result.reason or "", encoding="utf-8")
    artifact_path.write_text(json.dumps(result.to_dict(), indent=2), encoding="utf-8")


def _write_completed_deployment(
    result: DeploymentResult,
    artifact_path: Path,
    stdout_path: Path,
    stderr_path: Path,
    stdout_chunks: list[str],
    stderr_chunks: list[str],
) -> None:
    stdout_path.write_text("\n".join(stdout_chunks), encoding="utf-8")
    stderr_path.write_text("\n".join(stderr_chunks), encoding="utf-8")
    artifact_path.write_text(json.dumps(result.to_dict(), indent=2), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
