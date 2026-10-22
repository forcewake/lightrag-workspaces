# Security policy

LightRAG Workspaces is for a trusted operator or group sharing access to all
configured corpora. Workspace selection separates data routing and browser
history. It does **not** provide tenant security or per-workspace authorization.

## Access boundaries

- All workers inherit the same configured LightRAG authentication and provider
  credentials. A user authorized to use the service can select any discovered
  workspace. Workspace IDs are selectors, not secrets or access tokens.
- `GET /workspaces` and the HTML bootstrap expose workspace names, document
  counts, and processing state before upstream authentication. UI assets are
  also served by the gateway without an authentication check.
- The gateway selects and may start a worker before the upstream server checks
  the request's credentials. Upstream authentication alone does not protect
  discovery metadata or worker-start resource usage.
- Browser history and settings use workspace-specific local-storage keys on
  the same origin. Anyone with access to that browser profile can inspect
  them. The query-only interface is a presentation mode, not an access-control
  boundary.
- “Read only” pauses document mutations for preserved unfinished imports. Queries
  may still call external model providers and persist response-cache updates.
  It is not a filesystem read-only mode.

For access beyond a trusted local machine or network, put authentication and
TLS **in front of the entire origin**, including `/workspaces`, static UI paths,
and API paths. A private network or VPN can provide an additional boundary.
Configure request/body limits and concurrency appropriate to the host at that
boundary. Do not expose an unauthenticated instance directly to the internet.
Use separate deployments and credentials when users must not see each other's
corpora.

## Data and host access

Mount only the storage, inputs, and runtime directories needed by this service.
Do not mount the Docker socket or unrelated host directories. Workspace IDs are
validated and workspace-directory symlinks are rejected; local filesystem
writers still share the container's trust boundary.

Documents and retrieved text can contain instructions supplied by other people.
Treat model output as untrusted. Configured model or parser providers may receive
document content or query text; their policies and credentials are managed by
the operator. Keep real `.env` files, storage, logs, and diagnostic receipts out
of source control. Back up data before changing images or compatibility hooks.

## Supported baseline

The compatibility hooks target LightRAG **1.5.7** at the source revision and
image digest pinned in this repository, and nano-vectordb **0.0.4.3**. Other
versions are not supported merely because they start. Pinning enables a
repeatable baseline; it does not guarantee that dependencies have no
vulnerabilities. Rebuild and review dependency updates deliberately.

## Reporting a vulnerability

Use this repository's **Security → Report a vulnerability** option when it is
available. Include a minimal reproduction, affected revision or image digest,
expected behavior, and impact. Use synthetic data and redact credentials,
private addresses, workspace names, and document contents.

If private reporting is unavailable, open an issue asking the maintainer for a
private reporting channel. Do not put an exploit, sensitive data, or credentials
in the public issue. There is no guaranteed response time or commercial support
commitment.

Problems in unmodified LightRAG or another dependency should also be reported
through that upstream project's security channel. For LightRAG, see its
[security policy](https://github.com/HKUDS/LightRAG/security/policy).
