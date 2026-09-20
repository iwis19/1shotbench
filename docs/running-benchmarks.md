# Running Benchmarks

## CLI

Run one prompt against multiple model workspaces:

```sh
python -m bench.cli --prompt "Your task prompt" --models gpt claude gemini
```

Or read the prompt from a file:

```sh
python -m bench.cli \
  --task-dir experiments/frontend \
  --prompt-file experiments/frontend/PRD.md \
  --models gpt claude gemini glm kimi minimax
```

Useful options:

- `--task-dir experiments/frontend`
- `--prompt-file path/to/prompt.txt`
- `--mode sequential|parallel`
- `--max-concurrency 2`
- `--timeout-seconds 1800` or `--timeout-seconds 0` for no per-model timeout
- `--retries 1`
- `--warmup`
- `--label e2e-bench-1`
- `--no-task-rewrite` to skip the default per-model task file rewrite
- `--rewrite-timeout-seconds 600`
- `--deploy render` to deploy demos as Render image-backed web services after the benchmark run

Run a non-default task directory with:

```sh
python -m bench.cli --task-dir experiments/frontend --prompt "..." --models gpt claude
```

## Run Flow

The CLI flow is:

1. It reads the prompt from `--prompt` or `--prompt-file`.
2. It loads `*-workspace/bench.toml` files from `--task-dir`, `ONESHOT_BENCH_TASK_DIR`, or the default `experiments/frontend`.
3. It runs preflight checks for the selected model keys, the workspace folders, the `pi` executable, a sandbox backend, and any declared `required_skills`.
4. It refreshes task-local files matching `PRD*.md`, `TASK*.md`, `task*.md`, or `prompt*.md` into every configured workspace.
5. It creates a fresh `runs/<run_id>/` directory and writes the exact prompt to `prompt.txt`.
6. For each selected model, it first asks that same model to rewrite the shared task file(s) into a neutral equivalent restatement.
7. It validates rewritten files, stores copies under the run artifacts, and replaces the workspace-local task files before implementation starts.
8. It starts one implementation Pi subprocess per selected workspace, either sequentially or in parallel with `--max-concurrency`.
9. It writes per-model logs and a combined summary when the run finishes.

The task rewrite step keeps the implementation run from using wording that may advantage the model that originally authored the PRD.

## Web UI

Start the GUI server:

```sh
uvicorn bench.web:app --port 4010
```

Open:

```text
http://127.0.0.1:4010
```

The web server opens on `experiments/frontend` by default. You can switch between discovered experiment directories in the dashboard, or pin the initial directory from the shell:

```sh
ONESHOT_BENCH_TASK_DIR=experiments/evaluator uvicorn bench.web:app --port 4010
```

The UI can:

- pick configured model workspaces
- run models sequentially or in parallel
- stream stdout/stderr side by side
- show final duration and parsed token metrics
- load recent run history

## Run Artifacts

Artifacts are written to:

```text
runs/<run_id>/
```

Each run includes:

- `prompt.txt`: the prompt used for the run
- `<model>/stdout.log`: readable assistant output and tool markers
- `<model>/stderr.log`: Pi stderr
- `<model>/events.jsonl`: raw Pi JSON events
- `<model>/task-rewrite.json`: status, command metadata, and paths for the per-model task rewrite
- `<model>/task-rewrite.stdout.log`: raw Pi JSON output from the rewrite step
- `<model>/task-rewrite.stderr.log`: Pi stderr from the rewrite step
- `<model>/rewritten-task-files/`: copies of the rewritten task files used by the implementation step
- `<model>/result.json`: status, timing, command, paths, attempts, and token metrics for that model
- `<model>/workspace.sb`: generated macOS sandbox profile, when using `sandbox-exec`
- `<model>/workspace.bwrap.json`: generated Linux sandbox command metadata, when using `bwrap`
- `<model>/deployment.json`: deployment status and preview metadata, when deployment is attempted
- `<model>/deployment.stdout.log` and `<model>/deployment.stderr.log`: deployment logs with secrets redacted
- `deployments.json`: aggregate deployment results
- `deploy-staging/`: copied workspaces used for Docker builds
- `summary.json`: full machine-readable benchmark summary
- `summary.csv`: compact table for spreadsheets
- `summary.md`: compact Markdown summary

Statuses are process-level statuses. `completed` means Pi exited with code `0`; `failed` means a non-zero exit; `timeout` means the process exceeded `--timeout-seconds`. `--retries` reruns only failed or timed-out model subprocesses, and the final artifact files contain the last attempt's logs.

`--warmup` performs a short, unreported pre-run for each selected model before the measured run. Warmup logs are saved as `warmup.*` files inside each model artifact directory, but the benchmark summary uses the measured run.

## Metrics

1ShotBench records wall-clock duration and process status for every model. New runs execute Pi in JSON event mode and parse final assistant `usage` fields from `message_end` events. Raw Pi events are saved per model as `events.jsonl`, while readable output remains in `stdout.log`.

Older runs made before JSON event parsing may show zero token and cost fields because they were run with `--no-session` and text output did not include usage.

