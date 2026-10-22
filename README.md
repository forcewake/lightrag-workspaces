<p align="center"><img src="docs/assets/mark.svg" width="56" height="56" alt="LightRAG Workspaces mark"></p>

# LightRAG Workspaces

**Separate corpora. One place to work.**

A community extension for [HKUDS/LightRAG](https://github.com/HKUDS/LightRAG) that adds a workspace selector to the browser interface and workspace routing to the API. Store corpora in local files or **PostgreSQL with VectorChord**, with a separate worker process for each active workspace.

[Documentation](https://forcewake.github.io/lightrag-workspaces/) · [Installation](docs/getting-started/installation.md) · [API guide](docs/guides/api.md) · [Existing data](docs/guides/existing-data.md) · [Русский](README.ru.md)

![Illustrative request flow: one browser or API, a workspace gateway, and separate corpus workers](docs/assets/workspaces.svg)

*The diagram uses synthetic workspace names. This project is an independent extension, not an official LightRAG release.*

## What you get

- **A workspace selector** in the admin and retrieval interfaces, with separate browser conversations and settings for each workspace.
- **Two explicit API selectors:** `/workspaces/<id>/…` or the `LIGHTRAG-WORKSPACE` request header.
- **Workers loaded on demand**, a bounded active pool, idle eviction, and protection for active requests and streams.
- **Existing directory names preserved**, with startup compatibility writes disabled and unfinished legacy imports held for inspection.
- **Lower transient vector-loading memory** through a bounded loader for the pinned NanoVectorDB format. Memory admission remains an estimate; set a container limit.
- **PostgreSQL + VCHORDRQ storage**, including a verified importer for existing JSON, NanoVectorDB, and GraphML data. Exact workspace names and both API selectors stay the same.

## Quick start

You need Docker with the Compose plugin, enough memory for your corpora, and reachable LLM and embedding providers. The build downloads the pinned upstream sources and images.

```sh
git clone https://github.com/forcewake/lightrag-workspaces.git
cd lightrag-workspaces
cp .env.example .env
```

Edit `.env` with your provider endpoints, keys, model names, and embedding dimension. Keep the same embedding model and dimension when adopting existing indexes.

```sh
docker compose build
docker compose run --rm --no-deps --entrypoint python lightrag /opt/lightrag-multiworkspace/scripts/init_workspace.py default
docker compose up -d
```

Open **[localhost:9621/webui/](http://localhost:9621/webui/)**. Add another empty workspace with the same initializer:

```sh
docker compose run --rm --no-deps --entrypoint python lightrag /opt/lightrag-multiworkspace/scripts/init_workspace.py research
```

Refresh the page to see it in the selector. Unknown API workspace names return `404`; they never create a corpus.

```sh
curl http://localhost:9621/workspaces
curl http://localhost:9621/workspaces/research/health
curl http://localhost:9621/query \
  -H 'Content-Type: application/json' \
  -H 'LIGHTRAG-WORKSPACE: research' \
  -d '{"query":"What do the documents say?","mode":"mix"}'
```

If you enable API authentication, add the configured `X-API-Key` or bearer token to requests. See the [complete installation guide](docs/getting-started/installation.md) for configuration, resource limits, and exposure.

## Before bringing existing data

**Stop the old writer and take a verified backup first.** Test this extension against an independent copy before changing the original deployment. Do not change embedding models, rename folders, or allow two services to write the same corpus. Follow the [adoption guide](docs/guides/existing-data.md) and keep the [rollback procedure](docs/guides/backup-and-rollback.md) available.

An old unfinished import queue makes that workspace read-only for changes; compatible search remains available. Existing failed documents are not silently retried. Startup preservation does not make later explicit uploads, queries, or deletions byte-preserving operations. For a database deployment, follow the dedicated [PostgreSQL migration guide](docs/guides/postgresql.md); the file adoption recipe does not migrate a database.

## Compatibility and boundaries

| Area | Supported here |
| --- | --- |
| Upstream | LightRAG **1.5.7**, source and base image pinned |
| File storage | `JsonKVStorage`, `JsonDocStatusStorage`, `NanoVectorDBStorage`, `NetworkXStorage` |
| PostgreSQL storage | `PGKVStorage`, `PGDocStatusStorage`, `PGVectorStorage` with `VCHORDRQ`, `PGTableGraphStorage` |
| Deployment | One gateway process; one local worker process per active corpus |
| Models | One global provider configuration shared by all workspaces |
| Access control | Upstream authentication applies globally; workspaces are not per-user permissions |
| Other database backends | Neo4j, Qdrant, and unlisted combinations are outside this extension's supported contract |

Compose binds to `127.0.0.1` by default. A workspace ID is a routing choice, **not a security boundary**. Review [SECURITY.md](SECURITY.md) before sharing an instance or placing it behind a public endpoint.

## How it works

The gateway discovers direct child directories under the storage root. File mode opens corpus files; PostgreSQL mode accepts only verified registration manifests in a separate control root and checks live database identity and document counts. Selecting a workspace starts a pinned LightRAG process with its own input directory and database namespace. The gateway keeps a lease for each request, including the lifetime of a streamed response, and retires only workers proven idle.

The extension preserves exact public workspace names instead of passing them through upstream workspace-name normalization. PostgreSQL uses a deterministic hashed internal namespace, preventing names such as `meeting-notes` and `meeting_notes` from colliding. Migration is an explicit offline command; ordinary startup does not migrate data. Read the [architecture and limits](docs/reference/architecture.md).

## Development

```sh
uv sync --frozen
uv run --frozen python -m unittest discover -s tests -v
uv run --frozen python -m unittest test_worker -v
```

The Docker build rebuilds the upstream UI from pinned source with **Bun 1.3.3**, applies `frontend.patch`, and runs its tests, type checks, lint, and production build. The [development guide](docs/development.md) explains local frontend builds, regression tests, and documentation preview. See [CONTRIBUTING.md](CONTRIBUTING.md) to contribute and [the release checklist](docs/releasing.md) to prepare an upgrade.

## Attribution and license

Built on the work of the [HKUDS/LightRAG contributors](https://github.com/HKUDS/LightRAG). This repository distributes the extension and its upstream frontend patch under the [MIT license](LICENSE). See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for upstream attribution. Names and marks do not imply upstream endorsement.
