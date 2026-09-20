# Workspaces

## Layout

Task workspaces live in task-specific directories. The current tasks are under `experiments/`:

```text
experiments/
  frontend/
    PRD.md
    features.yaml
    gpt-workspace/
      bench.toml
    claude-workspace/
      bench.toml
    gemini-workspace/
      bench.toml

  evaluator/
    PRD.md
    features.yaml
    gpt-workspace/
      bench.toml

  nfcorpus-repro/
    PRD.md
    gpt-workspace/
      bench.toml
```

Future benchmark tasks can live as sibling directories with the same `*-workspace/bench.toml` structure.

## Workspace Config

Each workspace config supports:

```toml
name = "GPT workspace"
provider = "openai-codex"
model = "gpt-5.5"
thinking = "high"
tools = ["read", "bash", "edit", "write", "grep", "find", "ls"]
required_skills = ["install-anserini-fatjar", "anserini-cli", "anserini-reproduction"]

# Optional:
# system_prompt = "Custom system prompt"
# append_system_prompt = ["extra instructions", "path/to/file.md"]
```

For each selected model, `bench.toml` is converted into Pi CLI flags. For example:

```text
pi --mode json --print --no-session --provider anthropic --model claude-opus-4-7 --thinking high --tools read,bash,edit,write,grep,find,ls <prompt>
```

The runner sets the subprocess working directory to that model's workspace, so task files such as `./PRD.md` and any files the agent creates are local to that model. It also loads this project's `.env` into the subprocess environment before launching Pi.

## Task Files

Task files matching `PRD*.md`, `TASK*.md`, `task*.md`, or `prompt*.md` should live in the task directory, not the repo root. They are refreshed into every model workspace before each run. For example, `experiments/frontend/PRD.md` is available to every frontend agent as `./PRD.md` from inside its workspace.

## Creating Workspaces

Create or refresh the standard model workspace folders for a task with:

```sh
python scripts/create_agent_workspaces.py experiments/frontend
```

By default, the script creates:

- `gpt-workspace`
- `claude-workspace`
- `gemini-workspace`
- `deepseek-workspace`
- `glm-workspace`
- `kimi-workspace`
- `minimax-workspace`
- `mimo-workspace`

It writes missing `bench.toml` files using the current benchmark defaults and copies task-local files such as `PRD.md` into each workspace. It does not overwrite existing `bench.toml` files unless you pass `--force`.

## Workspace Isolation

1ShotBench runs each model from its own workspace directory and wraps each Pi subprocess in a platform sandbox. On macOS it uses `sandbox-exec`; on Linux it uses `bwrap` (bubblewrap). The sandbox allows normal process behavior but denies file reads and writes against the other configured model workspace directories.

That means a run from `glm-workspace` cannot inspect or modify `kimi-workspace`, `gpt-workspace`, and the other sibling model workspaces for the same task. Preflight fails if neither `sandbox-exec` nor a functional `bwrap` is available, because that isolation cannot be enforced.

This is workspace isolation, not a full container. Agents can still use allowed tools and the network according to the host environment and Pi configuration.

## Codex-Private Notes

Use `.codex-private/` for notes intended for Codex but not Pi benchmark agents. The directory is gitignored, and 1ShotBench adds it to every generated sandbox profile as a denied read/write path.

Do not put benchmark instructions for Pi agents there. Use task-local files such as `experiments/frontend/PRD.md` for agent-visible task prompts.

