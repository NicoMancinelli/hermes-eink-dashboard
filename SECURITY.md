# Security policy

## Deployment

The dashboard exposes operational metadata. Keep it on a trusted LAN or private overlay network. Bind to a specific interface, retain token authentication, and firewall the port to the Kindle or local subnet. Do not expose it directly to the internet.

The KUAL bundle contains the access token in `config.sh`. Treat generated ZIPs and the Kindle filesystem as sensitive. Rotate the token by replacing `~/.config/hermes-kindle-dashboard/token`, restarting the service, and rebuilding the KUAL bundle.

## Data minimization

The service does not return raw prompts, assistant messages, terminal output, tool output, environment variables, or credential files. It surfaces session metadata and allowlisted structured log summaries.

## Reporting

Please report vulnerabilities privately through GitHub Security Advisories rather than opening a public issue.
