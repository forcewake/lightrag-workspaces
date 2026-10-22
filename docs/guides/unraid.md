# Run on Unraid

Use a dedicated appdata directory and the same Compose contract as other Docker hosts. Preserve the existing template and authoritative data paths when adopting a running instance.

## Fresh installation

Choose an appdata location for this project, then clone the repository there. Keep the repository, `.env`, and data private to the host. The example below uses generic paths; replace them with your actual share layout.

```text
/mnt/user/appdata/lightrag-workspaces/
  compose.yaml
  .env
  data/
    rag_storage/default/
    inputs/
    runtime/
```

Use the [Docker Compose installation](../getting-started/installation.md) through your chosen Compose manager or the host terminal. The repository builds a local image from pinned sources; it does not depend on a prebuilt public package being available.

Compose binds the published port to host loopback by default. To reach it on your private LAN, deliberately choose the bind address and configure authentication and firewall or reverse-proxy controls. `BIND_ADDRESS=0.0.0.0` exposes the port on all host interfaces and requires a conscious network decision.

## Container template mapping

If using an Unraid Docker template instead of Compose, reproduce the built image's entrypoint and map:

| Container path | Host purpose |
| --- | --- |
| `/app/data/rag_storage` | Parent of file corpora, or a separate PostgreSQL control root after verified migration |
| `/app/data/inputs` | Parent directory of per-workspace uploads |
| `/app/data/workspace-runtime` | Separate private operational runtime directory |
| `9621/tcp` | Gateway; apply the intended host bind address and port |

Retain the image's gateway entrypoint. An old template's Post Arguments can accidentally invoke the stock server and bypass workspace routing. Set the provider and extension environment variables from the [configuration reference](../reference/configuration.md). Persist the memory limit and restart policy in the manager or template, not only in a temporary container command.

The health check should use `/workspaces`, which does not load a corpus. Do not make container liveness depend on a large cold workspace.

## PostgreSQL + VectorChord on Unraid

Use an existing PostgreSQL container with compatible pgvector/VectorChord packages and an already preloaded VectorChord library. Create a **new dedicated database** for this deployment; do not reuse an existing application database. Follow [the migration guide](postgresql.md) with an independent read-only source copy and a new control directory.

Connect LightRAG and PostgreSQL through a suitable Docker network, or use the intended host address and published PostgreSQL port. Preserve that network selection in the Unraid template. `POSTGRES_HOST=localhost` inside the LightRAG container does not reach the PostgreSQL container.

After verified import, map the new control directory to `/app/data/rag_storage`, retain the original `inputs` mount, set `LIGHTRAG_STORAGE_BACKEND=postgres`, and keep the exact default workspace and provider/auth configuration. The original JSON/GraphML tree and old container remain available for rollback. Save both the old and new Unraid templates privately. PostgreSQL backups become part of the recovery plan; preserving appdata alone is no longer sufficient.

## Adopting existing appdata

Follow [backup-first adoption](existing-data.md). Stop the current container, preserve its full template privately, and identify the **authoritative source** of every bind mount.

Unraid user shares may span cache and array devices. A path under `/mnt/cache` is not automatically a complete copy of the corresponding `/mnt/user` share. Verify that your backup contains every file and empty workspace directory visible through the live mount. Account for mover behavior and pool settings while testing and copying.

Keep the old container stopped with automatic restart disabled while the extension owns its data. Record how to restore the old template and image. Do not overwrite the old data with synthetic test files.

## Memory and shutdown

Set a Docker memory limit sized for the corpus and active worker count. Admission considers Linux host/cgroup headroom, but the container limit remains the backstop. Reducing `LIGHTRAG_MAX_ACTIVE_WORKSPACES` reduces the number of resident workers and changes each worker's provider budget share.

Not every Unraid Docker host enforces a separate swap limit. Check actual daemon support and host swap configuration; do not treat a requested swap option as evidence that the limit is enforced.

Allow a clean shutdown window for worker cleanup. Inspect container OOM state, restarts, and runtime logs after opening representative large corpora. A green gateway health check does not prove every corpus fits in memory or that a provider is working.
