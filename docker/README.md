# Running CI locally

`docker-compose.yml` runs the jobs from [`.github/workflows/ci.yml`](../.github/workflows/ci.yml)
on your machine. Use it to get a verdict without pushing a commit, and to reproduce a red
build when GitHub is the only place it fails.

Everything runs inside containers. No bench, database or site on the host is touched.

## Requirements

Docker Engine 25 or newer with the Compose plugin. The server jobs build a full bench
with ERPNext and Webshop, so allow roughly 8 GB of disk and 4 GB of memory per leg.

Run the commands from the app root (`apps/frappe_paystack`).

## Commands

```bash
# Frappe Linter and Code Quality: pre-commit, isort, black, flake8, pylint, mypy
docker compose -f docker/docker-compose.yml run --rm quality

# Semgrep Rules and Vulnerable Dependency Check
docker compose -f docker/docker-compose.yml run --rm security

# Server, Python 3.12 / frappe version-15
docker compose -f docker/docker-compose.yml run --rm server-v15

# Server, Python 3.14 / frappe version-16
docker compose -f docker/docker-compose.yml run --rm server-v16
```

Each command exits non-zero when its job fails, so they chain with `&&` and work as a
pre-push check.

The quality and security jobs finish in a couple of minutes. A server leg takes 20 to 40
minutes on a first run and less afterwards, since pip, uv and yarn caches persist in
named volumes.

Add `--build` after a change to anything in `docker/`.

## What runs where

| Service | Workflow job | Image |
| --- | --- | --- |
| `quality` | Frappe Linter, Code Quality | `python:3.12-bookworm` |
| `security` | Semgrep Rules, Vulnerable Dependency Check | `python:3.12-bookworm` |
| `server-v15` | Server (Python 3.12, frappe version-15) | `python:3.12-bookworm` + Node 24 |
| `server-v16` | Server (Python 3.14, frappe version-16) | `python:3.14-bookworm` + Node 24 |

`mariadb` and the two `redis` services start automatically for the server legs and stay
idle otherwise.

The server legs run the same
[`run-tests-with-coverage.sh`](../.github/scripts/run-tests-with-coverage.sh) the workflow
calls, so the 100% branch coverage gate applies here too.

## Differences from GitHub

**Your working tree is what runs.** GitHub tests the pushed commit. These containers copy
the checkout, including uncommitted and untracked files, and commit it onto a local
`ci-run` branch inside the container. Files ignored by git stay out. A file deleted in your
working tree but still committed will still be present.

**Semgrep scans everything.** The workflow runs `semgrep ci`, which reports only findings
new since the merge base. `docker/run-security-job.sh` runs a full scan, so it also lists
findings that predate the branch.

**Service hostnames.** GitHub publishes its service containers on `localhost`. Compose
resolves them by service name, so the generated bench config is repointed at `mariadb`,
`redis-cache` and `redis-queue`.

**The Cypress leg is not included.** The UI tests need a browser and a running bench.
Run them against a local bench instead.

## Resetting

```bash
# Drop the containers and the network
docker compose -f docker/docker-compose.yml down

# Also drop the database and the build caches
docker compose -f docker/docker-compose.yml down -v
```
