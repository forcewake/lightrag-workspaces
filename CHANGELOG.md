# Changelog

## 0.1.0 — 2026-10-22

Initial public source release of the independent LightRAG Workspaces extension.

- Workspace selection in the browser interface, with workspace-specific conversations and settings.
- API selection through `/workspaces/<id>/…` and the `LIGHTRAG-WORKSPACE` header, including scoped Swagger and OpenAPI paths.
- Bounded, on-demand worker processes, idle retirement, and request/stream leases.
- Exact directory identities, guarded startup compatibility writes, and protection against automatically resuming unfinished legacy imports.
- Bounded NanoVectorDB loading for the pinned native format and configurable memory admission.
- A source-only Docker Compose installation, English documentation, Russian quick start, and guides for Unraid, existing data, backups, and rollback.

The supported baseline is LightRAG 1.5.7 with the exact source and image pins in the repository, using JSON, NanoVectorDB, and NetworkX storage. Workspaces share authentication and provider configuration. They do not provide tenant authorization. Existing installations are not upgraded by publishing this release.
