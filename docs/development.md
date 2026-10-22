# Develop the extension

The repository contains a small Python gateway and worker layer, an upstream frontend patch, and a source-only Docker build. Do not edit generated UI bundles as the source of a change.

## Python checks

Use a current Python 3.12 environment, matching the pinned runtime family:

```sh
uv sync --frozen
uv run --frozen python -m unittest discover -s tests -v
uv run --frozen python -m unittest test_worker -v
```

The tests cover routing, invalid selectors, leases and streaming cleanup, idle eviction, provider-budget partitioning, workspace-path safety, startup preservation, and bounded-loader equivalence. They use synthetic fixtures rather than a private corpus or paid model endpoint. Read each test's dependencies before running an integration workflow.

The exact pinned Docker image remains the deployment oracle for upstream compatibility; a local Python test alone does not prove a new upstream release works.

The native Markdown header hook also has a no-network runtime check:

```sh
python scripts/smoke_table_headers.py --engine docker --image lightrag-workspaces:local
```

It uses the built image's packaged hook and real pinned parser, sidecar writer and paragraph chunker with synthetic Markdown/HTML. It checks small, header-only, repeated-value and oversized tables, token bounds and provenance without model calls or production mounts. `--module-file table_headers.py` is available to test a local hook against an older pinned image; only the command without that override verifies packaging in the new image.

## Frontend source

`frontend.patch` applies to upstream source commit `28ff1b05f2ac3f3e6fa14dd2cd33656579bd0c9c` (LightRAG 1.5.7). Use Bun **1.3.3** for the pinned frontend build.

```sh
./build_frontend.sh
```

The script obtains the immutable source revision, verifies and applies the patch, installs with the frozen lockfile, runs tests, type checks, lint, and the production build, then publishes assets into `webui/` only after success. It does not deploy a container.

To build without fetching another source clone:

```sh
LIGHTRAG_SOURCE_REPOSITORY=/path/to/local/LightRAG ./build_frontend.sh
```

The clone must contain the pinned commit. `BUN_BIN` can select the Bun executable. The optional first positional argument chooses a dedicated output directory.

For a frontend change, apply the patch to a clean pinned checkout, edit authored source and meaningful tests there, regenerate the patch, and rerun the complete build. Keep the exact upstream base and license notices. Do not commit generated bundles as a substitute for a reproducible source change.

## Container build

```sh
docker compose build
```

The multi-stage Dockerfile builds the patched frontend from pinned source with Bun 1.3.3 and copies it into the pinned upstream runtime. It does not rely on a previously generated local `webui/` directory. Review source pins and the runtime version guard together when upgrading.

To check that image without configuring providers or using existing data:

```sh
python scripts/smoke_container.py --engine docker --image lightrag-workspaces:local
```

The standard-library helper creates its own temporary volume and containers with networking disabled, no published ports, a 4 GiB memory ceiling, and synthetic empty indexes. It checks explicit initialization, discovery, URL/header selection through real workers, Swagger scoping, rejected selectors, preservation of the original fixture bytes, restart, and shutdown. It removes only the resources it created. Use `--engine podman` for an already-built local Podman image, or `--help` for memory and startup-timeout options.

This offline check exercises no model calls, ingestion, answer quality, or browser interaction. Run the separate workload checks below before adopting a real corpus.

## Documentation

```sh
python -m venv .venv-docs
. .venv-docs/bin/activate
pip install --require-hashes -r requirements-docs.txt
mkdocs serve
```

Open the local address printed by MkDocs. Before proposing changes:

```sh
mkdocs build --strict
```

Keep public examples synthetic. No private corpus names, credentials, real server addresses, container inspection dumps, or operational receipts belong in the repository. SVG diagrams are illustrative geometry, not deployment proof.

## Validate behavior on disposable data

For runtime integration, use an independent temporary storage/input/runtime tree and a separate localhost port. Exercise discovery, a synthetic upload, completed indexing, a grounded query, streaming, deletion, browser switching, and negative selectors. Use your deliberately configured provider account only when running a real-provider test.

For preservation changes, compare a baseline copy before reads, after worker loading, and after clean shutdown. Separate startup-only checks from later explicit writes. Include empty directories and old pending/processing states. Never use a production corpus as a write probe.

Contribution expectations, issue templates, and pull-request guidance are in the repository's `CONTRIBUTING.md`. An upstream upgrade requires the [release checklist](releasing.md).
