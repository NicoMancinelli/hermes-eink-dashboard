# Security policy

## Deployment

The dashboard exposes operational metadata. Prefer the default localhost bind or a private Tailscale address. If a non-Tailscale E-Ink device requires LAN access, bind to the host's specific LAN address, retain token authentication, and firewall the port to that device or local subnet. Never expose it directly to the internet or bind broadly without an equivalent firewall policy.

`/dashboard-data` accepts bearer authentication only. Query-string authentication is limited to the deprecated Kindle compatibility routes because BusyBox `wget` support varies. Uvicorn access logging is disabled to keep those legacy URLs out of logs.

The KUAL bundle contains the access token in `config.sh`. Treat generated ZIPs and the Kindle filesystem as sensitive. Rotate the token by replacing `~/.config/hermes-kindle-dashboard/token`, restarting the service, and rebuilding the KUAL bundle.

## Data minimization

The service does not return raw prompts, assistant messages, terminal output, tool output, environment variables, credential files, upstream responses, or exception text. It surfaces session metadata, allowlisted structured log summaries, and stable panel error codes.

## Reporting

Please report vulnerabilities privately through GitHub Security Advisories rather than opening a public issue.
