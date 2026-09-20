# Deployments

1ShotBench can deploy benchmark demos as Docker images for Render image-backed web services.

Deploy benchmark demos after a run:

```sh
python -m bench.deploy --run-id <run_id> --provider render
```

Or deploy automatically after a CLI benchmark:

```sh
python -m bench.cli \
  --task-dir experiments/frontend \
  --prompt-file experiments/frontend/PRD.md \
  --models gpt claude \
  --deploy render
```

For each attempted model, the harness stages the workspace under `runs/<run_id>/deploy-staging/`, uses an existing Dockerfile when present, or generates a generic Dockerfile for runnable Node/Next/Express or Python `server.py` apps. Unsupported or failed deployments still get `<model>/deployment.json` so the public demo table can show what happened.

## One-Time Render Setup

1. Create a Render web service for each task/model demo as an image-backed service.
2. Attach a GitHub Container Registry credential in Render so it can pull GHCR images.
3. Copy each service's deploy hook URL into `.codex-private/render.env`.

Configure GHCR and Render secrets outside the agent environment:

```sh
mkdir -p .codex-private
cat > .codex-private/render.env <<'EOF'
GHCR_USERNAME=...
GHCR_TOKEN=...
GHCR_OWNER=...
RENDER_DEPLOY_HOOK_FRONTEND_GPT=https://api.render.com/deploy/srv-...
RENDER_SERVICE_URL_FRONTEND_GPT=https://your-demo.onrender.com
EOF
```

Do not put `GHCR_TOKEN` or Render deploy hooks in this repo's `.env`; `.env` is passed to benchmark agents. The deploy harness also accepts these values from the shell environment.

The harness builds Docker images for `linux/amd64`, pushes them to:

```text
ghcr.io/<owner>/1shot-bench-<task>-<model>:<run_id>
```

It then triggers each Render deploy hook with `imgURL=<encoded-image-url>`.

Render web services must bind to `0.0.0.0` and the expected `$PORT`; generated Dockerfiles set sensible defaults, but agent-built apps still need to honor `PORT` for live demos.

See Render's docs for [Docker](https://render.com/docs/docker), [prebuilt image deploys](https://render.com/docs/deploying-an-image), [deploy hooks](https://render.com/docs/deploy-hooks), and [web services](https://render.com/docs/web-services).

## Deployment Artifacts

Deployment artifacts live under the run directory:

```text
runs/<run_id>/<model>/deployment.json
runs/<run_id>/<model>/deployment.stdout.log
runs/<run_id>/<model>/deployment.stderr.log
runs/<run_id>/deployments.json
runs/<run_id>/deploy-staging/
```

The deploy logs redact GHCR tokens and deploy-hook secrets.

