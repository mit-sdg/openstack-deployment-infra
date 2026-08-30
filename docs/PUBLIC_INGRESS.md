# Provide public DNS and HTTPS

The platform needs a public hostname with HTTPS for its health route. A future
management application and hosted projects will add management and project
hostnames under the same forwarding contract. You may use any DNS/TLS provider
that meets the requirements below.

Cloudflare Tunnel is the reference deployment. An institutional ingress service
is also valid when it provides the same behavior.

## Current and future completion boundaries

Automated setup can verify only the platform route:

```text
https://<domain>/healthz
```

The local controller is available only on admin's restricted Unix socket. The
management and authentication applications are not implemented, so do not treat
a healthy platform route as evidence that users can sign in or deploy projects.
Future management/project hostname behavior is specified in
[MANAGEMENT_APP_SPEC.md](MANAGEMENT_APP_SPEC.md).

## Required behavior

The public ingress service must:

1. resolve the configured platform hostname;
2. present a certificate trusted by ordinary browsers and HTTP clients;
3. forward requests to port 80 on the ingress host;
4. preserve the original HTTP `Host` header; and
5. allow `/healthz` to reach ingress.

For the future product, the provider must also cover the management hostname and
project hostnames of the form:

```text
<slug>.<domain>
```

A provider may use wildcard DNS/certificates or provision each hostname
separately. That future provisioning must finish before a project route can be
reported healthy.

## Request path

```text
browser or API client
        |
        | HTTPS
        v
public DNS/TLS service
        |
        | HTTP, original Host preserved
        v
ingress host:80 (Traefik)
        |
        | current static platform route, or future healthy Nomad service
        v
admin/platform health or project worker
```

Traefik selects a route from `Host`. Replacing the original header with an
origin hostname or address selects the wrong route or no route.

Although the current ingress image opens ports 80 and 443, generated routers
use the HTTP `web` entry point on port 80. Traefik does not currently request or
install public certificates. Do not send public HTTPS directly to port 443
without adding and testing a separate TLS configuration.

## Use Cloudflare Tunnel

Cloudflare Tunnel is enabled when an ingress host is created with a token. The
Cloudflare account and token are external inputs. Create a direct,
current-user-owned mode-`0600` file containing exactly one non-whitespace token
line, then set its path:

```bash
umask 077
export CLOUDFLARE_TUNNEL_TOKEN_FILE=/private/path/cloudflare-tunnel-token
install -m 0600 /path/from-your-private-secret-store "$CLOUDFLARE_TUNNEL_TOKEN_FILE"
test -f "$CLOUDFLARE_TUNNEL_TOKEN_FILE" && test ! -L "$CLOUDFLARE_TUNNEL_TOKEN_FILE"
test "$(stat -c '%a' "$CLOUDFLARE_TUNNEL_TOKEN_FILE")" = 600
```

`infra/openstack/apply_ingress.sh` validates the file, embeds it only in the
mode-`0600` temporary config-drive payload, and removes that payload after the
request. Do not store the token in `config/platform.json`, commit it, print it,
or place it directly in a command argument. Verify the result through DNS and
HTTPS, not by echoing the file.

Configure Cloudflare to route the platform hostname through the tunnel to the
Traefik HTTP origin. This repository does not manage account-specific DNS,
tunnel, or certificate configuration.

## Use another ingress service

Create ingress without cloudflared by setting:

```bash
export ENABLE_CLOUDFLARED=false
```

Configure the external service to forward to the configured ingress address,
preserve `Host`, and validate a certificate for the platform hostname.

The platform monitor calls its public check `cloudflare_tunnel`, although the
check makes an ordinary HTTPS request. With another provider, this is a
historical status label; it does not mean cloudflared is running.
`ENABLE_CLOUDFLARED=false` removes only the token requirement.

## Verify the current public path

```bash
PLATFORM_HOSTNAME=apps.example.edu
test "$(curl --fail --show-error --silent \
  "https://$PLATFORM_HOSTNAME/healthz")" = OK
```

The response body must be exactly:

```text
OK
```

This verifies public DNS, certificate validation, forwarding, host-based
routing, ingress, and the platform health route. A request directly to the
ingress address does not verify the intended public path.

Do not add a project health check to the current end-user acceptance evidence.
The controller service is local and hosted, but project routes become a
supported user acceptance surface only after the management and authentication
applications are implemented.

## Diagnose common failures

| Symptom | Check | Correction |
| --- | --- | --- |
| Platform name does not resolve | Query the exact configured hostname | Add the matching DNS record in the external provider |
| Certificate name mismatch | Inspect the certificate's covered names | Issue a certificate for the exact hostname or matching wildcard |
| Traefik returns no route | Compare the forwarded `Host` with the configured platform hostname | Preserve the original host instead of replacing it with the origin address |
| `/healthz` works by ingress address but not publicly | Test DNS, TLS, and provider origin routing separately | Correct the provider hostname/origin mapping |
| Setup rejects a Cloudflare token | Check direct-file ownership, mode `0600`, and one-line token shape without printing it | Install a fresh provider token and rerun the same setup checkpoint |

See [CONFIGURATION.md](CONFIGURATION.md) for `domain`, `recoveryDomains`, and
static ingress routes. See [CONTROL_PLANE_CONTRACT.md](CONTROL_PLANE_CONTRACT.md)
for the current operator/controller boundary.
