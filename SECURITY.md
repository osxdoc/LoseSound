# Security policy

## Supported versions

Only the latest release receives security fixes.

| Version | Supported |
| --- | --- |
| 1.x | yes |
| < 1.0 | no |

## Reporting a vulnerability

Please do not open a public issue for security problems. Send a short report to
**spam@g5v.de** with:

- affected version (`/api/health`)
- a description of the issue and its impact
- reproduction steps, if possible

You can expect an acknowledgement within a few days. Please give me a reasonable amount of time to
ship a fix before publishing details.

## Scope

LoseSound is designed for a trusted home network:

- Bind it to your LAN only. If you expose it through a reverse proxy, add authentication there.
- Set `API_TOKEN` to protect `POST`/`DELETE` endpoints. Without it, every device on the LAN can
  change stations and preset buttons.
- Station URLs are validated (http/https only, no link-local metadata addresses), but the relay
  fetches arbitrary URLs by design. Do not let untrusted users add stations.
- Secrets such as stream tokens are redacted from logs unless `LOG_URL_QUERY=1` is set explicitly.
- The container runs as UID 10001 with all capabilities dropped and `no-new-privileges`.
