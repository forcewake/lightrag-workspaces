# Install with Docker Compose

Build the extension from source, configure your providers, and create the first empty workspace. The default instance is reachable at `http://localhost:9621` on the Docker host.

## Prerequisites

- Docker Engine or Docker Desktop with the Compose plugin and BuildKit.
- Network access to GitHub, the pinned container images, frontend packages, and your providers during the relevant build or request.
- An LLM endpoint and an embedding endpoint supported by the pinned upstream release.
- Memory sized for your corpus. The example container limit is **8 GiB**, with **2 GiB** reserved by memory admission. This is a starting configuration, not a minimum or a capacity promise.

If you have an existing corpus, use [adoption](../guides/existing-data.md) instead of importing it into an unreviewed configuration.

## Clone and configure

```sh
git clone https://github.com/forcewake/lightrag-workspaces.git
cd lightrag-workspaces
cp .env.example .env
chmod 600 .env
```

Edit `.env` before starting. Fill in the LLM and embedding binding, endpoint, key, model, and embedding dimension. The example uses the OpenAI-compatible protocol; it does not provide credentials or select a provider account for you.

`EMBEDDING_DIM` must match the vectors produced by your embedding model. Matching dimensions alone does not establish model compatibility with an old index. Preserve the original model identity, configuration, and dimension when adopting one.

Review `LIGHTRAG_MAX_ACTIVE_WORKSPACES`, the provider concurrency budgets, and `LIGHTRAG_MEMORY_LIMIT` together. Every configured provider concurrency budget must be at least the maximum active worker count. See the [configuration reference](../reference/configuration.md).

## Build and initialize

```sh
docker compose build
docker compose run --rm --no-deps --entrypoint python lightrag /opt/lightrag-multiworkspace/scripts/init_workspace.py default
docker compose up -d
```

The build starts from a pinned upstream image. A separate source stage fetches the pinned UI commit, applies `frontend.patch`, installs its frozen dependency lockfile with Bun 1.3.3, and runs tests, type checks, lint, and the production build. Build time and downloads depend on the machine and cache.

The initializer creates an explicitly named empty corpus directory. Discovery never creates a workspace just because an API client supplies a name.

## Confirm the installation

```sh
docker compose ps
curl http://localhost:9621/workspaces
curl http://localhost:9621/workspaces/default/health
```

`/workspaces` is a cheap discovery and gateway liveness endpoint; it does not load a corpus. The scoped `/health` request starts the selected worker if needed. A cold load can take up to 240 seconds and may return `503` if memory or worker slots are unavailable.

Open [the admin interface](http://localhost:9621/webui/). The retrieval-focused interface is [localhost:9621/workspace/](http://localhost:9621/workspace/). Continue with [your first workspace](first-workspace.md).

## Decide who can reach it

Compose binds to `127.0.0.1` by default. For another machine, prefer an authenticated TLS reverse proxy or a private tunnel. If you deliberately change `BIND_ADDRESS`, configure upstream authentication and network controls first.

Set `LIGHTRAG_API_KEY` for API-key authentication, or configure upstream account authentication with a fresh `TOKEN_SECRET`. Workspace selection itself grants no per-workspace permissions. The discovery list and browser shell are served by the gateway; treat workspace names and counts as instance-visible metadata.

For a remote SSH host, a local tunnel keeps the default bind:

```sh
ssh -N -L 9621:127.0.0.1:9621 your-server
```

## Data layout

```text
data/
  rag_storage/
    default/            # Saved corpus files
    research/           # Another independent corpus
  inputs/
    default/            # Uploaded source files
    research/
  runtime/              # Worker locks and private operational logs
```

Back up `rag_storage`, `inputs`, your private `.env`, Compose overrides, and the source revision together. Keep runtime outside the corpus tree. See [backup and rollback](../guides/backup-and-rollback.md).
