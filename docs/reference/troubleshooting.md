# Troubleshooting

Start with discovery, the selected workspace's health, and its private runtime log. A healthy gateway only proves the gateway can serve discovery.

```sh
curl http://localhost:9621/workspaces
curl http://localhost:9621/workspaces/research/health
docker compose logs --tail=100 lightrag
```

Worker logs are stored under the mounted runtime tree. They can contain document or provider details; sanitize them before reporting an issue.

## The workspace is missing

Check that its directory is a direct child of the configured storage root, uses a supported exact identifier, and is not a symlink. Refresh an already-open page. If the workspace is meant to be new, run the explicit initializer. Unknown API identifiers never create directories.

If existing files sit directly in the storage root, review the [adoption layout](../guides/existing-data.md); discovery expects named child directories.

In PostgreSQL mode, a directory also needs a verified `.postgres-workspace.json` registration in the new control root. An empty directory made by the file initializer alone is not a PostgreSQL workspace. Use the explicit [PostgreSQL migration/registration procedure](../guides/postgresql.md); do not write a manifest by hand.

## The page opens, but a request returns 503

The selected worker may still be loading, all slots may be busy, or memory admission may have refused its estimated load. Wait for current work to finish and retry. Inspect the selected worker's log if it exits during startup.

Check container memory usage, OOM state, restarts, and host memory. Reduce `LIGHTRAG_MAX_ACTIVE_WORKSPACES` to reduce residency, or size the hard memory limit for a measured representative corpus. Increasing a client timeout does not solve insufficient memory.

On extension versions before **0.2.3**, PostgreSQL graph startup repeats schema DDL. A concurrent reader or writer can block that redundant `ALTER TABLE`; the worker log then shows `LockNotAvailableError` / `canceling statement due to lock timeout`, and the gateway reports that the selected workspace could not start. Version 0.2.3 validates the existing graph schema without that DDL. Upgrade the application after taking a consistent backup and finishing active processing. This fix needs no document reindexing or PostgreSQL restart. Other causes of 503 still require the checks above.

If the updated worker reports an incompatible graph schema, inspect the specific missing contract and restore or repair it through a separately verified offline operation. Do not delete the registration or let startup initialize a replacement corpus.

The cold-start deadline is 240 seconds. A reverse proxy or API client with a shorter timeout can disconnect first. Configure the whole path for that wait and for long streamed responses.

## A workspace returns 409

Read its discovery metadata:

- **Saved dimension mismatch:** restore the original embedding configuration or carry out a separate reviewed re-indexing migration. Matching dimensions alone is not model-identity proof.
- **Unreadable metadata:** inspect a backup and identify the exact malformed or incompatible record.
- **Earlier unfinished imports:** compatible search remains available, while mutations are paused. Do not edit statuses solely to suppress the guard.
- **PostgreSQL unavailable or registration mismatch:** restore connectivity and verify the configured database, identity token, exact workspace mapping, embedding model/dimension, and migration readiness. Do not point the worker at an empty replacement database or copy a manifest from another installation.

See [existing-data adoption](../guides/existing-data.md) before attempting recovery.

## PostgreSQL startup or vector search fails

Check that the container can reach `POSTGRES_HOST:POSTGRES_PORT`, that the intended dedicated database contains both `vector` and `vchord`, and that its VectorChord library was preloaded by the database service. Confirm migration verification and the actual VCHORDRQ indexes rather than relying on a successful application process start. Use `VCHORDRQ` with the recorded embedding identity; changing to `HNSW` does not support a 3072-dimensional full-precision vector index.

If custom probes are configured, their hierarchy must match the index's partition options. Start without custom build/probe overrides until representative recall and latency checks justify them. Inspect bounded database activity and index-build resource use without publishing connection strings, vectors, source contents, or query text.

## The worker refuses its concurrency budget

Each explicitly configured LLM, embedding, rerank, or role-level provider budget must be at least `LIGHTRAG_MAX_ACTIVE_WORKSPACES`. Lower the worker count or set a deliberate larger original budget that your provider can support. The extension divides the budget; it does not multiply provider capacity.

## I cannot find the API tab

Open `/workspaces/research/docs` directly. This is the selected worker's Swagger UI. The extension scopes its schema, static assets, and request URLs. Verify `/workspaces/research/openapi.json` if Swagger cannot load.

A missing API navigation tab in the pinned frontend does not mean the API endpoint is disabled.

## I get 401 or 403

Check the API key or bearer token and the upstream account configuration. Workspace names are not credentials. A workspace startup error should be treated separately from an authentication failure.

## Provider requests fail

Check the selected binding, endpoint, model, dimension, provider credentials, network reachability, and account limits. A provider at `localhost` from inside the container refers to the container itself. Use a host or network address reachable from that container.

Do not paste keys or private environment files into a public issue. The extension preserves the upstream provider contract; consult the pinned upstream provider documentation for provider-specific options.

## The first response has no useful context

Confirm that the file finished processing in the selected workspace and ask about a known fact in its content. Inspect extracted text and references. A successful upload receipt or a healthy provider does not establish retrieval quality.

## Streaming arrives all at once

Use `curl --no-buffer`, parse NDJSON lines, and review reverse-proxy response buffering. The endpoint is not SSE. A TCP or HTTP chunk is not necessarily a complete JSON record.

## I changed an option and nothing happened

Recreate the container to apply environment changes. Some upstream settings affect only future ingestion. Browser parameters may be saved per workspace and override query defaults. Review the selected page's parameters before changing server settings again.

## Report a reproducible issue

Include the extension revision, image identity, host architecture, selected storage adapters, sanitized environment variable names and non-secret values, exact endpoint/status, and a small synthetic reproduction. State whether it is a cold or warm worker. For memory problems include the container limit, OOM/restart counters, and representative file sizes without corpus contents.

Keep security reports out of public issues and follow the repository security policy.
