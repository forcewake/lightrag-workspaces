# Separate corpora. One place to work.

LightRAG Workspaces adds a workspace selector and explicit API routing to the pinned LightRAG server. Each corpus keeps its own directory and starts in a separate worker process when you open it.

<picture>
  <source media="(max-width: 600px)" srcset="assets/workspaces-mobile.svg">
  <img src="assets/workspaces.svg" alt="Illustrative flow from a browser or API through one gateway to separate research and handbook corpus workers">
</picture>

*An illustrative request path with synthetic workspace names.*

[Install a fresh instance](getting-started/installation.md){ .md-button .md-button--primary }
[Bring existing data](guides/existing-data.md){ .md-button }

## Follow your next task

| Your task | Start here |
| --- | --- |
| Run a fresh installation | [Docker Compose installation](getting-started/installation.md) |
| Create a corpus and import your first file | [First workspace](getting-started/first-workspace.md) |
| Select a workspace from an application | [API and streaming](guides/api.md) |
| Keep an existing index | [Backup-first adoption](guides/existing-data.md) |
| Run it on Unraid | [Unraid guide](guides/unraid.md) |
| Understand memory and compatibility | [Architecture and limits](reference/architecture.md) |
| Read in Russian | [Быстрый старт](getting-started/quickstart.ru.md) |

## Know the supported boundary

This is an independent community extension for **LightRAG 1.5.7**, with source and image pinned. It supports the file-backed `JsonKVStorage`, `JsonDocStatusStorage`, `NanoVectorDBStorage`, and `NetworkXStorage` combination.

Workspace selection separates corpus data and worker processes. It does **not** create per-workspace user permissions. Authentication and provider configuration are shared across the instance. The default Compose configuration listens only on localhost.

Existing data needs a verified backup and a test on a copy before adoption. Old unfinished imports stay paused. The extension performs no silent migration and makes no compatibility claim for a moving upstream release.

## Build on the original project

LightRAG Workspaces extends [HKUDS/LightRAG](https://github.com/HKUDS/LightRAG). The upstream server, query behavior, and document processing remain the underlying engine. The extension adds explicit selection, worker lifecycle management, a bounded vector loader, and the corresponding browser changes.

Source, issues, and contributions live in [forcewake/lightrag-workspaces](https://github.com/forcewake/lightrag-workspaces). The project is MIT licensed; upstream attribution ships with the repository.
