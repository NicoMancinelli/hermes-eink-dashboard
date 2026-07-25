# Security policy

## Deployment

The dashboard exposes operational metadata. Prefer the default localhost bind or a private Tailscale address. If a non-Tailscale E-Ink device requires LAN access, bind to the host's specific LAN address, retain token authentication, and firewall the port to that device or local subnet. Never expose it directly to the internet or bind broadly without an equivalent firewall policy.

`/dashboard-data` accepts bearer authentication only. Query-string authentication is limited to the deprecated Kindle compatibility routes because BusyBox `wget` support varies. Uvicorn access logging is disabled to keep those legacy URLs out of logs.

The KUAL bundle has two build modes:

- **Template mode** (default): the bundle contains placeholder values
  (`HOST_IP=PLACEHOLDER.lan`, `DASHBOARD_TOKEN=PLACEHOLDER_TOKEN`). Safe to
  publish as a release artifact. After installing on the Kindle, run
  `bin/post_install.sh` to inject the real host and tokens.
- **Personal mode** (`--inject-tokens`): the bundle contains the real
  read and control tokens from `~/.config/hermes-kindle-dashboard/`.
  Do NOT publish this artifact — anyone who downloads it can hit your
  host's `/control` endpoints with write access.

`bin/post_install.sh` writes the tokens to `config.sh` on the Kindle with
`chmod 600`. The Kindle's filesystem is encrypted at rest on modern
firmware, but treat it as sensitive.

Rotate tokens by replacing `~/.config/hermes-kindle-dashboard/token` and
`~/.config/hermes-kindle-dashboard/control_token`, restarting the service,
and rebuilding + re-installing the KUAL bundle.

## Interactive controls security

Control endpoints (`POST /control`, `GET /control/events`) enforce a dual-token security model:
- Write actions require a separate `--control-token`. If `control_token` is unset or empty, control endpoints return `503 Service Unavailable` rather than degrading to the read token.
- Control endpoints accept bearer tokens in request headers only (never via query strings).
- Actions are validated against an explicit allowlist with prefix matching (`alert.dismiss.*`, `context.set.*`).
- Replay protection enforces a 60s nonce TTL and ±30s server timestamp validation window, with per-action rate limiting.
- Config parser DoS is mitigated by capping `actions.yaml` at 1MB.
- Error details are sanitized to generic error codes (`invalid_timestamp`, `invalid_nonce`, `invalid_payload`, `forbidden`, `rate_limited`) to prevent reflecting user-supplied input or internal exception tracebacks.
- All HTTP responses enforce security headers: `Cache-Control: no-store`, `X-Content-Type-Options: nosniff`, and `X-Frame-Options: DENY`.
- Action handlers run in a `ThreadPoolExecutor` (4 workers) so a slow
  workflow does not block the asyncio event loop or starve other
  requests. Handlers have a 30s default timeout.

## Data minimization

The service does not return raw prompts, assistant messages, terminal output, tool output, environment variables, credential files, upstream responses, or exception text. It surfaces session metadata, allowlisted structured log summaries, and stable panel error codes.

## Reporting

Please report vulnerabilities privately through GitHub Security Advisories rather than opening a public issue.

