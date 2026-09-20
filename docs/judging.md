# Judging Runs

1ShotBench includes three web-app evaluation paths:

- `bench.llm_judge`: Playwright evidence plus an LLM verdict.
- `bench.codex_judge`: Codex CLI as the evaluator in a disposable copied workspace.
- `bench.pi_judge`: Pi as the evaluator in a disposable copied workspace.

All judge paths write artifacts under `evals/<eval_id>/`.

## LLM Judge

Judge agent-built web apps with PRD-derived feature checks, Playwright evidence, and an LLM judge. The evaluator can either read a checked-in `features.yaml` or ask the judge model to generate feature checks from the PRD before running the browser layer.

One-time setup for the browser layer:

```sh
cd bench/llm_judge && npm install && npx playwright install chromium
pip install -r requirements.txt
```

Run a full evaluation with a curated feature file:

```sh
python3 -m bench.llm_judge \
  --project experiments/evaluator/gpt-workspace \
  --features experiments/evaluator/features.yaml \
  --prd experiments/evaluator/PRD.md \
  --label gpt-evaluator
```

Or generate the feature file from the PRD at evaluation time:

```sh
python3 -m bench.llm_judge \
  --project experiments/evaluator/gpt-workspace \
  --prd experiments/evaluator/PRD.md \
  --label gpt-evaluator-generated
```

Before starting the app, the evaluator builds setup context from the PRD, implementation README files, manifests, and any repo-local skills referenced there. The judge model can propose concrete setup commands from that context. The runner then executes only general allowlisted setup commands, such as package installs, project-local setup scripts, explicit environment assignments, downloads with project-local output paths, and smoke-check commands.

For each feature, the browser layer first runs scripted evidence steps, captures page text, ARIA, screenshots, errors, and visible interactive elements, then optionally asks the judge model for a short follow-up browser plan. The final verdict receives the collected browser evidence plus setup context/results, but it must still judge from evidence rather than assume success.

Useful options:

- `--base-url http://127.0.0.1:3000` and `--no-start` when the app is already running
- `--dry-run` to skip LLM calls for judging/planning/generation where possible
- `--judge-model` or env `WEB_EVAL_JUDGE_MODEL`
- `--setup never` to skip README setup commands
- `--no-agentic-evidence` to use only scripted Playwright steps
- `--max-generated-features 8` to cap PRD-generated feature checks

## Codex Judge

The Codex judge uses Codex CLI as the evaluator, runs it in a disposable copied workspace, and gives it a dedicated judging skill stored under `judge_skills/web_judge/`.

Before using the Codex judge, make sure Codex CLI is installed and logged in:

```sh
codex login
```

If you are using a ChatGPT subscription-backed Codex login rather than API keys, this login step is required before `bench.codex_judge` can run successfully.

Run the Codex judge with:

```sh
python3 -m bench.codex_judge \
  --project experiments/evaluator/gpt-workspace \
  --features experiments/evaluator/features.yaml \
  --prd experiments/evaluator/PRD.md \
  --label gpt-codex-judge
```

Or ask Codex to generate the features from the PRD:

```sh
python3 -m bench.codex_judge \
  --project experiments/evaluator/gpt-workspace \
  --prd experiments/evaluator/PRD.md \
  --label gpt-codex-judge-generated
```

Useful options:

- `--base-url http://127.0.0.1:3000` and `--no-start` when the app is already running
- `--keep-judge-workspace` to preserve the disposable copy at `evals/<eval_id>/judge-workspace/`
- `--judge-workspace-root path/to/root` to control where the disposable copy lives instead
- `--codex-sandbox workspace-write` to override the default sandbox, though this may prevent local servers or Chromium from running
- `--model <codex-model>` to choose the Codex model
- `--search` to enable live web search for the judge if you explicitly want that mode

## Pi Judge

The Pi judge mirrors the Codex judge flow: it copies the coding agent workspace into a disposable judge workspace, gives Pi the dedicated judging skill in `judge_skills/web_judge/`, and writes the same `evals/<eval_id>/` artifacts.

Run the Pi judge with:

```sh
python3 -m bench.pi_judge \
  --project experiments/evaluator/gpt-workspace \
  --features experiments/evaluator/features.yaml \
  --prd experiments/evaluator/PRD.md \
  --label gpt-pi-judge
```

Or ask Pi to generate the features from the PRD:

```sh
python3 -m bench.pi_judge \
  --project experiments/evaluator/gpt-workspace \
  --prd experiments/evaluator/PRD.md \
  --label gpt-pi-judge-generated
```

Useful options:

- `--base-url http://127.0.0.1:3000` and `--no-start` when the app is already running
- `--keep-judge-workspace` to preserve the disposable copy at `evals/<eval_id>/judge-workspace/`
- `--judge-workspace-root path/to/root` to control where the disposable copy lives instead
- `--provider <pi-provider>` and `--model <pi-model>` to choose the Pi judge backend
- `--thinking <level>`, `--system-prompt`, `--append-system-prompt`, and `--tools read,bash,edit,write,grep,find,ls` to control Pi CLI options

## Evaluation Artifacts

Artifacts are written to `evals/<eval_id>/`:

- `summary.json`, `report.md`, `run.json`, `setup.json`, `setup-context.json`
- `generated-features.yaml` when `--features` is omitted
- `evidence/<feature>.json` plus raw scripted/agentic browser packets
- `judgments/<feature>.json`
- `screenshots/` and `jobs/` from the browser layer

Correctness is `passed / total * 100`; `uncertain` counts as not passed.

When a web eval result looks wrong, inspect the artifacts before changing code:

1. `evals/<id>/setup.json`
2. `evals/<id>/setup-context.json`
3. `evals/<id>/run.json`
4. `evals/<id>/evidence/*.json`
5. `evals/<id>/judgments/*.json`
6. screenshots and app logs if available

The disposable judge workspace may receive dependency installs and temporary runtime artifacts, but the judge should not modify source-like app files to make an implementation pass.

