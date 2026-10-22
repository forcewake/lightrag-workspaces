# Create your first workspace

A workspace is an existing, directly named child directory under the storage root. Create it explicitly, then import and query documents through that workspace.

## Create an empty workspace

From the repository directory:

```sh
docker compose run --rm --no-deps --entrypoint python lightrag /opt/lightrag-multiworkspace/scripts/init_workspace.py research
```

Use simple identifiers such as `research`, `handbook`, or `project-notes`. The supported public identifier grammar is an ASCII letter or digit followed by up to 127 ASCII letters, digits, dots, hyphens, or underscores. Names are case-sensitive. Spaces, path separators, dot segments, and symlink aliases are rejected.

Refresh the interface to update its list. The gateway re-discovers directories for requests; an already-open browser receives its selector list when the page loads.

## Upload a small document

Select **research** before uploading. In the admin interface, open **Documents**, upload a supported file, and wait until processing finishes. Provider usage and costs are determined by your configured services.

To use a synthetic first file:

```sh
cat > workspace-example.md <<'EOF'
# Example handbook
The observatory review is held on Tuesday.
The review owner is Mira Solvik.
EOF

curl -F 'file=@workspace-example.md' \
  http://localhost:9621/workspaces/research/documents/upload
```

Upload acceptance means the file was queued, not that indexing has completed. Check the Documents status or the selected workspace's document endpoints before querying. Add authentication headers when enabled.

## Ask a question

Open **Retrieval** and ask, “When is the observatory review?” Or use the API:

```sh
curl http://localhost:9621/workspaces/research/query \
  -H 'Content-Type: application/json' \
  -d '{"query":"When is the observatory review?","mode":"mix","include_references":true}'
```

Use a question answered by your imported content. A working request without useful source content is not a retrieval-quality test.

## Keep the workspace explicit

The URL includes `/workspaces/research/`. API responses include `LIGHTRAG-WORKSPACE` and `X-LightRAG-Workspace` identity headers. Check these when wiring an application.

Switching away retains that workspace's browser conversation and settings; switching back restores them. This history is local to the browser and interface, not a server-side synchronized chat service.

To remove or reorganize a workspace, stop its writers and plan a backup-first filesystem operation. This extension exposes discovery and selection, not an API for creating, renaming, or deleting workspace directories.
