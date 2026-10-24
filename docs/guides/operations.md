# Backups, restore drills, and application updates

`scripts/workspace_ops.py` is a host-run operations tool for PostgreSQL deployments. It captures matching database and source-file snapshots, proves a restore in isolated containers, and updates the application while retaining its previous container. It never restores an old snapshot into the live database.

The first upgrade from a version without `/maintenance` must use the [cold backup procedure](backup-and-rollback.md). The tool requires the maintenance contract introduced in 0.3.0; a missing endpoint is an error, not permission to copy a running writer. Existing private backups remain useful recovery material, but are not automatically relabeled as verified tool bundles.

## Requirements and scope

- Linux, Python 3.12 or 3.13, Docker, GNU `cp`, and `curl`; run as an operator able to preserve file ownership and extended attributes.
- An already registered PostgreSQL deployment with pgvector/VectorChord, one application container, and explicit control/input bind mounts.
- Host paths visible at the same absolute paths to the operator. Backups must be outside the source trees. Use a private directory (`0700`) and configuration (`0600`).
- Enough space for a database dump, independent file copies, and restore fixtures. Plan checks at least twice the database size plus source file sizes and 2 GiB. A large restore needs additional room for indexes and temporary files.
- One operator at a time. Use a host lock around scheduled and manual operations. Do not run an importer, schema migration, or another backup tool during snapshot capture.

This adapter rejects anonymous/named volumes, multiple application networks, static endpoint settings, and namespace sharing during updates. Adapt and review those layouts explicitly. If Unraid user-share paths and cache/disk paths expose the same files under different names, use the authoritative mount paths and verify there are no other writers through an alias; ordinary path resolution cannot prove FUSE share equivalence.

Backups contain raw container configuration, connection secrets, source material, and a maintenance owner receipt. Keep the entire bundle private. Console output contains operation paths and verification status, not raw environment values.

## Configure and inspect

Example private configuration; replace the names and paths for your installation:

```json
{
  "format": 1,
  "application": "lightrag",
  "postgres_container": "lightrag-postgres",
  "postgres_user": "postgres",
  "database": "lightrag",
  "origin": "http://127.0.0.1:9621",
  "backup_root": "/srv/private/lightrag-backups",
  "mounts": {
    "control": "/app/data/rag_storage",
    "inputs": "/app/data/inputs"
  },
  "drain_timeout_seconds": 300,
  "reflink": "auto",
  "drill_memory": "4g",
  "drill_app_memory": "8g"
}
```

`mounts` contains container destinations. The tool discovers their host sources from the current container. `unraid_template` is optional: set it to the existing container XML template if its Repository should be updated with the image. It must already agree with the running container. `docker_socket` defaults to `/var/run/docker.sock`.

```bash
python3 scripts/workspace_ops.py --config /srv/private/operations.json plan
```

`plan` reads container identities, workspace inventory, storage paths, free space, and possible competing writable mounts. It does not create a backup, enter maintenance, or restart anything.

It also proves database binding: an application-side read-only connection carries a fresh random session label which must be visible through the configured PostgreSQL container. The actual database marker, database name, complete workspace inventory, and every control manifest must agree. This catches a wrong but otherwise valid LightRAG database even when Docker aliases or published LAN ports are involved. The backup root must be outside all application and PostgreSQL mounts.

After reviewing that result, add `expected_application_id` and `expected_postgres_id` with the exact 64-character container IDs to the private configuration. Backup and update fail closed if either identity differs. An application update changes its container ID; after verification, explicitly refresh the expected application ID and any scheduler configuration checksum. Container restarts alone keep the same ID.

## Capture a consistent backup

```bash
python3 scripts/workspace_ops.py --config /srv/private/operations.json backup
```

The sequence is:

1. Enter an owned, persistent maintenance gate. New corpus traffic, including queries that write caches, is blocked. Wait for in-flight requests, document processing, and worker queues to drain. Reject active external database transactions and other containers with writable overlapping mounts.
2. Export a PostgreSQL `REPEATABLE READ` snapshot and keep its transaction alive. Copy the complete control and input trees independently, preserving empty directories, ownership, modes, timestamps, extended attributes, symlinks, and hardlink relationships. Verify the source and copied manifests while writes remain paused.
3. Release maintenance. The application may accept new work while `pg_dump --snapshot` and full row fingerprints read the earlier frozen database view. This shortens the write pause; its actual duration is recorded. Large file inventories or a filesystem without reflinks can still make the pause substantial.
4. Read and decompress the entire custom archive with `pg_restore --file=/dev/null`, hash every bundle file, and retain the schema, counts, extensions, and sorted row SHA-256 fingerprints. Mark the bundle verified only after all checks pass.

Since 0.3.1, `sorted-jsonb-row-sha256-v2` hashes each canonical UTF-8 JSONB row with PostgreSQL's built-in SHA-256, sorts only its 64-character hexadecimal digest using `C` collation, and hashes the resulting digest lines while streaming. Duplicate rows remain duplicate digest lines; each line and the complete row count are checked. This reduces sort width for large documents and vectors without raising `temp_file_limit` or changing database settings outside these read-only sessions. Very large row counts can still require temporary space.

V2 pins `TimeZone=UTC`, `DateStyle=ISO,YMD`, `IntervalStyle=postgres`, `bytea_output=hex`, and `extra_float_digits=3` inside each read-only transaction. Restore drills use the algorithm recorded in the bundle. Existing `sorted-jsonb-copy-sha256-v1` bundles retain their original serialization and complete-row sorting behavior; v1 and v2 hashes are never compared as equivalent. Unknown algorithms fail closed. No `pgcrypto` extension is required.

The database and files belong to one recovery point. Changes accepted after maintenance is released intentionally belong to a later backup. The exported snapshot keeps older row versions alive until dumping and fingerprinting finish; monitor storage on a heavily written database. The tool does not claim zero downtime, a restore test, or provider-level retrieval success from a backup alone.

Runtime locks and logs are not authoritative data and are excluded from the file pair. Container configuration, image IDs, PostgreSQL database marker, installed extension versions, preload setting, and workspace inventory are retained. A failed bundle remains private with a failed-stage receipt and is not eligible for retention deletion.

If an operator process is killed before releasing maintenance, inspect the recorded operation before using:

```bash
python3 scripts/workspace_ops.py --config /srv/private/operations.json \
  release-maintenance --operation /srv/private/lightrag-backups/backup-OPERATION
```

The saved owner token must match the active gate. There is no timeout that silently permits writes into an unfinished snapshot.

## Prove an independent restore

```bash
python3 scripts/workspace_ops.py --config /srv/private/operations.json \
  verify-bundle --bundle /srv/private/lightrag-backups/backup-OPERATION

python3 scripts/workspace_ops.py --config /srv/private/operations.json \
  restore-drill --bundle /srv/private/lightrag-backups/backup-OPERATION
```

The drill verifies the bundle again, starts a newly named PostgreSQL container from the recorded exact image, and uses an empty owned data directory. It reproduces the recorded `shared_preload_libraries`, restores without source ownership/ACLs, and compares every public table's row count/hash, columns, constraints, indexes, and extension versions. The database has the recorded name inside this separate PostgreSQL instance; no restore connection targets the source container.

PostgreSQL omits invalid source indexes from `pg_dump`. The comparison allows only an absent index recorded with `indisvalid=false` in the source fingerprint, listing its exact definition and flags in `restore-comparison.json` and the drill result. Every remaining object and row must match; a missing valid index or any changed index fails. This exception records an existing source defect and does not repair, drop, or rebuild anything in the live database.

If the recorded container runs as a non-root user, an isolated read-only probe resolves that user's UID/GID in the exact image. Only the newly created empty fixture data/runtime directory is assigned that ownership before launch. Existing source data, backup copies, and other host directories are never recursively chowned.

For a PostgreSQL image whose entrypoint starts as root, the probe resolves the image's `postgres` OS account and assigns the new mounted parent to it. This also allows access to nested PGDATA after the entrypoint drops privileges. Images with a different privilege-drop convention require an explicit adapter and fail if that account cannot be resolved.

The fixture has `--network none`, no published ports, and no source-data mounts. The restored app shares only the fixture's network namespace. Provider keys are replaced with useless synthetic values, authentication credentials are removed, and the original model names/dimensions and workspace IDs remain. The restored files are byte/metadata checked before removing the maintenance gate in the isolated working copy. Runtime gets a fresh empty directory.

Each workspace must serve scoped health, UI HTML, Swagger, and OpenAPI with the correct workspace identity. A second database fingerprint checks the effects of starting the restored workers: every row and all remaining schema must still match. Native startup may recreate only a previously reported omitted invalid index with the exact same table, name and definition, now both valid and ready. Each such addition is listed in `after-service-comparison.json`, and the drill reports `schema_unchanged=false`; any other schema change fails. This exception applies only to the isolated restored database and makes no repair to the live source.

This is a structural restore and routing check: it does not simulate browser clicks or call a model. Use the [quality checks](quality.md) separately for source-grounded retrieval and inspect UI switching in a real browser before declaring a deployment accepted.

Successful fixture containers are removed; private restored files and receipts remain. `--keep-containers` keeps them stopped. Failed fixtures are stopped and retained for diagnosis. The tool checks ownership labels before cleanup. Review their receipts and free space before removing any drill directories yourself.

## Update and roll back the application

Build from the exact source revision or pull a verified published image by digest. Run the repository's container/worker/PostgreSQL checks against that image and create a private acceptance receipt:

```json
{
  "passed": true,
  "image_id": "sha256:THE_ACTUALLY_TESTED_IMAGE_ID",
  "database_compatible": true,
  "application_only": true
}
```

These fields are an operator attestation backed by retained test results. The command validates the image identity; it cannot manufacture or independently prove your compatibility review. Do not use this route for upstream/schema/storage migrations.

First review the proposed update:

```bash
python3 scripts/workspace_ops.py --config /srv/private/operations.json update \
  --bundle /srv/private/lightrag-backups/backup-OPERATION \
  --image ghcr.io/OWNER/lightrag-workspaces@sha256:VERIFIED_DIGEST \
  --acceptance /srv/private/image-acceptance.json
```

Add `--apply` to execute. Optionally add repeated `--quality-suite /srv/private/quality/example.json` and `--quality-report-dir /srv/private/quality-results`. Suites are validated against the configured origin before any swap. Existing report files are never overwritten. Queries run sequentially after readiness/routing checks, may use model services and write caches, and are not automatically retried.

The updater verifies that the live container still matches the backup's exact configuration. It enters/drains maintenance, records current database/file fingerprints, and stops only the app gracefully. It retains and renames the old container with restart disabled, clones its environment, bind mounts, limits, published ports, and network aliases, and starts the tested image on the same current database. The persistent maintenance gate prevents outside writes during comparison. No database container is restarted or restored.

It checks rows, schema, files, workspace counts/IDs, database continuity, and image/configuration identity before releasing maintenance. Scoped UI/API checks then run; optional quality checks are reported separately. The updater changes only Repository in an explicitly configured Unraid XML template and retains its previous bytes.

If a step fails, read `operation.json` and maintenance state. The tool deliberately does not overwrite current data or perform an automatic database downgrade. Plan and execute an application-only rollback:

```bash
python3 scripts/workspace_ops.py --config /srv/private/operations.json \
  rollback --operation /srv/private/lightrag-backups/update-OPERATION

python3 scripts/workspace_ops.py --config /srv/private/operations.json \
  rollback --operation /srv/private/lightrag-backups/update-OPERATION --apply
```

Rollback drains/stops this operation's candidate, retains it for diagnosis, and restores the previous container/name/restart policy/template. It runs against current data. New uploads accepted after cutover are not reverted. A version requiring a schema downgrade needs an explicitly reviewed recovery procedure instead.

## Schedule on Unraid without host Python

### Private native runtime

An administrator can use a private Python runtime on the host instead of granting a container the Docker socket. For the currently pinned Debian/Python runtime, copy `/usr/local/bin/python3.12`, `/usr/local/lib/python3.12`, `/usr/local/lib/libpython3.12.so.1.0`, and the matching `libssl.so.3`/`libcrypto.so.3` from a **stopped, newly owned** container created from the verified image digest. Put the interpreter in a private `bin/` and libraries/stdlib in its sibling `lib/`; remove the extraction container afterward. Do not replace Unraid system libraries or execute the image entrypoint for extraction.

Check the image's actual paths/dependencies first: these paths are a property of the pinned image, not a general Python installer. Test imports of `ssl`, `hashlib`, `subprocess`, `urllib.request`, and extended-attribute support on the host before use. This extraction was exercised on an Unraid host with its existing loader/libc and the matching private SSL libraries. A different host's libc may be incompatible; fail rather than modifying its system runtime.

```bash
LD_LIBRARY_PATH=/srv/private/python-runtime/lib \
  /srv/private/python-runtime/bin/python3.12 \
  /srv/lightrag-workspaces/scripts/workspace_ops.py \
  --config /srv/private/operations.json plan
```

The host account still needs Docker administrator permissions. Tests should use a fixture-only command allowlist; production operation authorization remains tied to the configured application, PostgreSQL instance, and backup directory. Use this native command in the locked schedule wrapper below if the operator-container socket access is inappropriate for your environment.

A native schedule wrapper can expose no arguments and bind the reviewed script/configuration by checksum. Replace all example values before installing it; keep the runtime manifest alongside the wrapper and pin its source image digest.

```bash
#!/bin/bash
set -euo pipefail
umask 077
test "$#" -eq 0
exec 9>/srv/private/lightrag-operations.lock
flock -n 9 || exit 0
printf '%s\n' \
  'REVIEWED_SCRIPT_SHA256  /srv/lightrag-workspaces/scripts/workspace_ops.py' \
  'REVIEWED_CONFIG_SHA256  /srv/private/operations.json' \
  | sha256sum --check --status
readonly ops_python=/srv/private/python-runtime/bin/python3.12
readonly ops_script=/srv/lightrag-workspaces/scripts/workspace_ops.py
readonly ops_config=/srv/private/operations.json
export LD_LIBRARY_PATH=/srv/private/python-runtime/lib
"$ops_python" -I -B "$ops_script" --config "$ops_config" backup
"$ops_python" -I -B "$ops_script" --config "$ops_config" retention --keep 3 --apply
```

This wrapper only invokes backup and retention. It does not forward shell arguments, expose a Docker command endpoint, install schedules, or update the application. Python isolated mode excludes untrusted Python import/environment paths. For a separate update command with quality suites, use the documented repository entrypoint so its sibling quality modules can be imported.

### Operator container alternative

Use a separate operator container. Do not run the updater inside the app container it will stop. The pinned application image supplies Python; mount the host's Docker CLI read-only, its socket, a read-only checkout of the scripts, and private configuration. Bind the backup directory read-write and both authoritative source paths read-only at the **same absolute paths** used by Docker.

Example wrapper skeleton, with fictional paths to replace after `plan`:

```bash
#!/bin/bash
set -euo pipefail
umask 077
exec 9>/srv/private/lightrag-operations.lock
flock -n 9 || exit 0

run_ops() {
  docker run --rm --network none --memory 1g --cpus 1 \
    --mount type=bind,src=/usr/bin/docker,dst=/usr/local/bin/docker,readonly \
    --mount type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock \
    --mount type=bind,src=/srv/lightrag-workspaces/scripts,dst=/checks,readonly \
    --mount type=bind,src=/srv/private/operations.json,dst=/srv/private/operations.json,readonly \
    --mount type=bind,src=/srv/private/lightrag-backups,dst=/srv/private/lightrag-backups \
    --mount type=bind,src=/srv/lightrag/control,dst=/srv/lightrag/control,readonly \
    --mount type=bind,src=/srv/lightrag/inputs,dst=/srv/lightrag/inputs,readonly \
    --entrypoint python ghcr.io/OWNER/lightrag-workspaces@sha256:VERIFIED_DIGEST \
    /checks/workspace_ops.py --config /srv/private/operations.json "$@"
}

run_ops backup
run_ops retention --keep 3 --apply
```

The Docker socket grants host-level control; this is a trusted administrator tool. The operator's `--network none` still allows Docker-exec communication over the mounted socket. For an update with HTTP quality suites, use a separately reviewed operator network that can reach the explicitly pinned origin. For restore drills, Docker needs the drill paths visible through the same backup-root bind.

Use the installed Unraid scheduling mechanism to run this wrapper daily at 03:00 in the operator's chosen host timezone (for example, `Europe/Amsterdam`). A host crontab entry is `0 3 * * * /absolute/private/backup-wrapper.sh`; installation/persistence depends on the Unraid mechanism already in use. Do not assume `CRON_TZ` support. Route exit status and private logs to the existing monitoring channel. A JSON schedule record can retain the wrapper path, image digest, host timezone, cron expression, retention count, last success, and last restore drill. Verify one actual run before enabling recurrence.

Retention is opt-in and restricted to this tool's owned, verified `backup-*` bundles under `backup_root`. The newest N, all failed bundles, and every bundle referenced by a drill or update are protected. Referenced containers also block deletion. Legacy backups, private migration folders, and restore directories are never pruning targets. Preview first:

```bash
python3 scripts/workspace_ops.py --config /srv/private/operations.json retention --keep 3
```

## Optional independent copy

An Unraid-only backup does not survive loss of that host/storage. When a separate destination is ready, mount it privately and copy one verified bundle:

```bash
python3 scripts/workspace_ops.py --config /srv/private/operations.json copy \
  --bundle /srv/private/lightrag-backups/backup-OPERATION \
  --destination /separate-mounted-storage/lightrag/backup-OPERATION
```

The destination must not exist or overlap the source backup root or any live application/PostgreSQL mount. The copy is verified against complete file/metadata manifests and bundle checksums; it fails if the destination cannot preserve them. The tool cannot establish that a mounted path is physically off-host. No remote transfer, scheduler, or retention is enabled by this command alone.

## Published images and source fallback

The version workflow runs from an existing `vMAJOR.MINOR.PATCH` tag matching `pyproject.toml`. It tests and publishes **linux/amd64** to GHCR with version and source-revision tags, then pulls the published digest and reruns offline artifact checks. It does not claim ARM support. Deploy the recorded `ghcr.io/...@sha256:...` digest; tags remain mutable registry names.

If packages are disabled or publishing fails, source installation remains supported: check out the same tag/revision, build locally, run the same image-bound tests, and retain the resulting image ID. A successful source build is not evidence that a registry image was published.
