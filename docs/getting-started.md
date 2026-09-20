# Getting Started

## Requirements

- Python 3.11+
- Pi coding agent available on `PATH`
- workspace sandbox support: `sandbox-exec` on macOS or `bwrap` on Linux
- provider credentials for the models you run
- any task-specific Pi skills installed or installable by the agent

Install Python dependencies from the repo root:

```sh
pip install -r requirements.txt
```

## Pi Auth And Keys

Pi supports subscription logins and API-key providers.

For subscriptions, run Pi interactively and use `/login`:

```sh
pi
# then type /login and choose ChatGPT Plus/Pro (Codex), Claude Pro/Max, or GitHub Copilot
```

Pi stores login credentials in:

```text
~/.pi/agent/auth.json
```

For API keys, either use Pi's `/login` flow, edit `~/.pi/agent/auth.json`, export environment variables in your shell, or put them in this project's gitignored `.env` file. 1ShotBench loads `.env` before starting each agent process.

Common `.env` entries:

```sh
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...
GEMINI_API_KEY=...
ZAI_API_KEY=...
MINIMAX_API_KEY=...
MOONSHOT_API_KEY=...
KIMI_API_KEY=...
```

Auth file example:

```json
{
  "anthropic": { "type": "api_key", "key": "sk-ant-..." },
  "openai": { "type": "api_key", "key": "sk-..." },
  "google": { "type": "api_key", "key": "..." },
  "zai": { "type": "api_key", "key": "..." },
  "minimax": { "type": "api_key", "key": "..." },
  "moonshotai": { "type": "api_key", "key": "..." }
}
```

Pi resolves credentials from `~/.pi/agent/auth.json` before environment variables. The `key` value in `auth.json` can also name an environment variable or start with `!` to run a shell command such as a password-manager lookup.

## Skills And Web Access

Pi's built-in tools are coding tools: `read`, `bash`, `edit`, `write`, `grep`, `find`, and `ls`. The Pi CLI help for version `0.75.1` does not list a built-in web-search tool. Agents can still use `bash` for commands and skill installers when network access is available, and Pi supports installing packages with:

```sh
pi install <source>
```

For task skills, the default track is a prepared-environment benchmark: required task skills and docs are installed before the run, and preflight fails if they are missing. This keeps the comparison focused on whether each model can use the same resources to complete the same implementation task.

For the current Anserini PRDs, preinstall the Anserini skill set into the project before running:

```sh
scripts/install_anserini_skills.sh
```

This copies Anserini's `.agents/skills` directory into this repo's `.agents/skills`. Pi discovers `.agents/skills` from the current workspace and ancestor directories, so every model workspace sees the same local copies.

The workspace configs declare these required skills:

- `install-anserini-fatjar`
- `anserini-cli`
- `anserini-reproduction`

Preflight fails if any declared `required_skills` are missing from `.agents/skills`, `.pi/skills`, `~/.pi/agent/skills`, or `~/.agents/skills`.

Keep prepared-environment and bootstrap/autonomy benchmark tracks separate in run labels and result tables. Mixing them would confound implementation quality with web search, network reliability, package installation, and documentation discovery.

