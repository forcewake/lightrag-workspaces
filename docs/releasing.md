# Prepare a release

Release from a reviewed commit with an exact upstream source pin and runtime image digest. This checklist describes maintainer work; it does not claim an unrun check has passed.

## Review the version boundary

- Read upstream changes since the pinned release and inspect the affected startup, storage, auth, parser, query, and UI contracts.
- Update the runtime guard, source revision, image digest, frontend patch, and dependency pins as one coherent change.
- Check the four supported storage adapters and the NanoVectorDB serialization/normalization behavior.
- Review attribution and license notices for new or changed upstream material.

## Build and verify

- Run the complete Python test suite and source frontend build.
- Build the Docker image from a clean checkout without a pre-existing generated UI directory.
- Run the docs strict build and verify links in both README languages.
- Test a new synthetic corpus through upload, processing, query, references, streaming, and deletion.
- Test a disposable copy of a legacy corpus, including byte-preserving startup and clean shutdown before any explicit write.
- Test old pending/processing queue protection and incompatible metadata/dimensions.
- Open a representative large corpus under a measured hard memory limit; record OOM and restart state.
- Check both browser interfaces, workspace-specific history/settings, auth failure behavior, retry states, and different workspaces in separate tabs.
- Confirm no credentials, private documents, client/corpus names, screenshots, operational receipts, or machine paths enter the source archive or image layers.

## Publish honestly

Write a changelog around changed behavior and practical compatibility limits. Distinguish unit checks, synthetic integration, browser checks, and workload-specific validation. Do not reuse an old runtime verification as proof for a changed image.

Tag the reviewed extension revision. If distributing container images, record immutable digests and supported architectures, preserve source/notice links, and test the published artifact independently. The source-build path must remain documented even if a registry package is provided later.

Keep the previous image and a verified cold backup for each deployment upgrade. Release publication and deployment are separate actions; a published tag does not mean an existing host has been upgraded.
