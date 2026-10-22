# Contributing

Useful contributions include reproducible bug reports, clearer documentation, and focused fixes to workspace selection, preservation, and worker lifecycle behavior.

Read the [architecture and supported boundaries](docs/reference/architecture.md) before changing runtime behavior. Use the [development guide](docs/development.md) for the locked Python environment, frontend source build, and disposable container smoke check.

## Propose a change

1. Open an issue describing the use case for a substantial feature or upstream upgrade.
2. Make a focused change against `main`. Edit authored code and `frontend.patch`, not generated UI bundles.
3. Add a regression test for meaningful behavior changes. Use synthetic fixtures and no paid model calls in CI.
4. Run the relevant checks and explain their results in the pull request. Update both language quick starts when installation changes.

Preserve exact workspace names, provider-budget bounds, active request leases, and the compatibility guard. Do not silently migrate data, resume unfinished imports, or broaden the supported backend list without tests and documentation. Cross-version upgrades need the [release checklist](docs/releasing.md).

## Keep reports safe

Never include a real `.env`, corpus, provider key, private endpoint, raw container inspection dump, operational receipt, or screenshot containing private data. Redact diagnostics and reproduce with disposable workspaces. Follow [SECURITY.md](SECURITY.md) for vulnerabilities rather than posting exploit details publicly.

## Licensing and conduct

Contributions are accepted under this repository's [MIT license](LICENSE). Preserve upstream attribution and third-party notices. Be respectful, explain evidence, and keep discussion focused on the work. Maintenance is community-driven with no guaranteed response time.
