from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_TASK_DIR = "experiments/frontend"
TASK_FILE_PATTERNS = ("PRD*.md", "TASK*.md", "task*.md", "prompt*.md")
REQUIRED_SKILLS = [
    "install-anserini-fatjar",
    "anserini-cli",
    "anserini-reproduction",
]
TOOLS = ["read", "bash", "edit", "write", "grep", "find", "ls"]


@dataclass(frozen=True)
class WorkspaceSpec:
    key: str
    name: str
    provider: str
    model: str
    thinking: str = "high"
    tools: list[str] = field(default_factory=lambda: TOOLS.copy())
    required_skills: list[str] = field(default_factory=lambda: REQUIRED_SKILLS.copy())

    @property
    def directory_name(self) -> str:
        return f"{self.key}-workspace"

    def to_toml(self) -> str:
        return "\n".join(
            [
                f'name = "{self.name}"',
                f'provider = "{self.provider}"',
                f'model = "{self.model}"',
                f'thinking = "{self.thinking}"',
                f"tools = {_toml_list(self.tools)}",
                f"required_skills = {_toml_list(self.required_skills)}",
                "",
            ]
        )


WORKSPACES = [
    WorkspaceSpec("gpt", "GPT workspace", "openai-codex", "gpt-5.5"),
    WorkspaceSpec("claude", "Claude workspace", "anthropic", "claude-opus-4-7"),
    WorkspaceSpec("gemini", "Gemini workspace", "google", "gemini-3.1-pro-preview"),
    WorkspaceSpec("deepseek", "DeepSeek workspace", "deepseek", "deepseek-v4-pro"),
    WorkspaceSpec("glm", "GLM workspace", "zai", "glm-5.1"),
    WorkspaceSpec("kimi", "Kimi workspace", "moonshotai", "kimi-k2.6"),
    WorkspaceSpec("minimax", "MiniMax workspace", "minimax", "MiniMax-M3"),
    WorkspaceSpec("mimo", "Xiaomi MiMo workspace", "xiaomi", "mimo-v2.5-pro"),
]


def _toml_list(values: list[str]) -> str:
    return "[" + ", ".join(f'"{value}"' for value in values) + "]"


def _display_path(path: Path) -> Path:
    try:
        return path.relative_to(ROOT_DIR)
    except ValueError:
        return path


def discover_task_files(task_dir: Path) -> list[Path]:
    files: dict[str, Path] = {}
    for pattern in TASK_FILE_PATTERNS:
        for path in task_dir.glob(pattern):
            if path.is_file():
                files[path.name] = path
    return [files[name] for name in sorted(files)]


def create_workspaces(task_dir: Path, force: bool) -> list[str]:
    task_dir.mkdir(parents=True, exist_ok=True)
    task_files = discover_task_files(task_dir)
    actions: list[str] = []

    for spec in WORKSPACES:
        workspace_dir = task_dir / spec.directory_name
        workspace_dir.mkdir(parents=True, exist_ok=True)

        config_path = workspace_dir / "bench.toml"
        if force or not config_path.exists():
            config_path.write_text(spec.to_toml(), encoding="utf-8")
            actions.append(f"wrote {_display_path(config_path)}")
        else:
            actions.append(f"kept  {_display_path(config_path)}")

        for task_file in task_files:
            copy_path = workspace_dir / task_file.name
            if copy_path.exists() or copy_path.is_symlink():
                if copy_path.is_dir():
                    actions.append(f"skip  {_display_path(copy_path)} is a directory")
                    continue
                copy_path.unlink()
            copy_path.write_text(task_file.read_text(encoding="utf-8"), encoding="utf-8")
            actions.append(f"copy  {_display_path(copy_path)}")
    return actions


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create 1ShotBench model workspace folders for a task."
    )
    parser.add_argument(
        "task_dir",
        nargs="?",
        default=DEFAULT_TASK_DIR,
        help=f"Task workspace directory to create. Default: {DEFAULT_TASK_DIR}",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing bench.toml files with the default model specs.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    task_dir = Path(args.task_dir)
    if not task_dir.is_absolute():
        task_dir = ROOT_DIR / task_dir
    for action in create_workspaces(task_dir=task_dir, force=args.force):
        print(action)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
