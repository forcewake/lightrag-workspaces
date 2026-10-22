# Back up and roll back

A rollback consists of restoring the previous application configuration and, when needed, the matching cold data snapshot. Keeping an old image alone does not recover files changed after cutover.

For PostgreSQL deployments, the current corpus is in the database. Keep a consistent PostgreSQL backup, its pgvector/VectorChord versions and restore instructions, the matching control manifests and inputs, private connection settings, and the exact application revision. Test restoration into a separate database and deployment. A copy of the control directory alone is not a corpus backup.

## What to retain

| Item | Why it matters |
| --- | --- |
| Complete `rag_storage` tree | Corpus metadata, vectors, graph, chunks, and caches |
| Complete `inputs` tree | Uploaded source files and parser artifacts |
| Private `.env` and Compose overrides | Model identity, endpoints, dimensions, auth, and mount settings |
| Source revision and exact image identity | Reproduce the version that owned the snapshot |
| Empty directories and file manifest | Preserve empty workspaces and verify the copy |
| Old container configuration and image | Recreate the prior process without guessing |
| PostgreSQL database backup, extension versions, identity token, and control registrations | Required in PostgreSQL mode; restore the database and its matching registration as one recovery set |

Logs may include document or provider details. Keep backups private and protect credentials separately. Runtime locks are operational files, not corpus data; never mount a corpus directory as runtime.

## Take a cold backup

Finish active work and stop the writer cleanly before copying. Back up the whole trees using a tool that preserves the metadata you need. Verify the copied data against a manifest or checksum list, and test restoration into a separate location.

On a copy-on-write filesystem, a reflink can reduce snapshot cost while remaining independently writable. Hard links share file data and are unsuitable for a staging write test. An unverified backup is only a proposed recovery path.

## Application-only rollback

If the extension has only opened data and a verified comparison confirms that the old application's required data remains unchanged:

1. Stop the extension and ensure its workers have exited.
2. Preserve its current configuration and runtime logs for diagnosis.
3. Restore the previous mounts, environment, and exact image or container.
4. Start the old service and verify its health, counts, and workload behavior.

Do not run the old and new writers together. This extension's lock cannot stop an unrelated older deployment.

## Restore a data snapshot

If explicit uploads, queries, deletions, or other writes occurred, first decide which snapshot is the recovery target. Restoring an older snapshot loses changes made after it.

1. Stop all writers and retain the current data tree under a distinct recovery name.
2. Restore the verified cold snapshot into the intended paths; preserve both storage and inputs as a matching pair.
3. Restore the configuration and application version recorded with that snapshot.
4. Verify the restored manifest, empty workspace directories, counts, and service health before normal use.

Never delete the failed tree merely to make the restore fit. Keep a recovery copy until the restore and any post-snapshot data reconciliation are complete.

## Returning from PostgreSQL to a file snapshot

Stop the PostgreSQL application before restoring the old file deployment. Preserve the current database and input tree under separate recovery names, then restore the old image, file-mode settings, and matching file/input snapshot. Keep the old file corpus separate from the PostgreSQL control root.

The importer does not export new PostgreSQL changes back to JSON. Returning to the old snapshot omits uploads, deletions, graph changes, and cache changes made after cutover. Decide the recovery point and reconcile required newer data before discarding anything. Retain the PostgreSQL recovery copy until that decision is complete.

## Upgrade safely

Pin the extension revision and keep the previous image. Build the next version separately, read its compatibility notes, and exercise a copy of representative corpora before cutover. Changing the upstream image or UI commit requires a source and preservation review; removing a pin is not an upgrade procedure.
