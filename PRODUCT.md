# LightRAG Workspaces

<!-- impeccable:product-schema 1 -->

## Platform
web

## Stack
Python gateway and worker launcher around pinned LightRAG 1.5.7. The upstream React interface is extended with a source patch and built with Bun 1.3.3. Documentation uses Material for MkDocs.

## Users
Self-hosters and developers who maintain several independent document corpora and want one browser interface and one API address. This audience and use scene are inferred from the requested public release and the implementation, rather than market research.

## Product Purpose
Select an existing corpus in the interface or in an API request while retaining its exact data directory. A successful installation makes selection explicit, keeps ingestion and streaming requests attached to the selected corpus, and explains compatibility limits before existing data is adopted.

## Positioning
A community extension for the pinned LightRAG server. The gateway discovers corpus directories and starts an individual upstream process for each active corpus. It adds selection and process lifecycle management; it is not an upstream LightRAG release.

## Operating Context
Docker Compose on a private host, with optional Unraid adoption. LLM, embedding, and reranking providers are configured globally. Existing storage must be backed up before first use. Documentation and examples contain synthetic corpus names only.

## Capabilities and Constraints
- One browser selector and workspace-aware API routes and headers.
- Existing direct child directories are discovered; an unknown API identifier never creates data.
- Supported storage is JsonKVStorage, JsonDocStatusStorage, NanoVectorDBStorage, and NetworkXStorage.
- A bounded worker pool loads corpora on demand and preserves busy requests and streams.
- Old unfinished ingestion queues remain paused, with searches allowed where metadata is compatible.
- Workspace selection is not user authorization or a tenant security boundary. Provider configuration is shared.
- The current patch is pinned to LightRAG 1.5.7; upgrades require compatibility review.

## Brand Commitments
The confirmed name is LightRAG Workspaces. The requested public documentation is professional, restrained, complete, and easy to use. It must clearly credit HKUDS/LightRAG and distinguish maintained facts from illustrations.

## Evidence on Hand
Runtime and regression-test source, a pinned frontend patch, and a synthetic deployment workflow. No customer identities, private server addresses, credentials, real corpus contents, or private operational screenshots may be published.

## Product Principles
- Preserve data and make selection explicit.
- Explain limits at the point they matter.
- Prefer reproducible source builds over a moving upstream tag.
- Make the first successful install and the rollback path equally findable.
