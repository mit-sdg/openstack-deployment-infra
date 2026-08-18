# Provide public DNS and HTTPS

Each deployed application needs a public hostname with HTTPS. You may use any
DNS or TLS provider that meets the forwarding contract below.

The reference deployment uses Cloudflare Tunnel. You can instead use an
institutional ingress service, such as one that assigns a hostname like
`apps.example.edu`, if it provides the same behavior.

## Required behavior

The public ingress service must:

1. resolve the platform hostname and every application hostname;
2. present a certificate trusted by ordinary browsers and HTTP clients;
3. forward requests to port 80 on the ingress host;
4. preserve the original HTTP `Host` header; and
5. allow `/healthz` and application health paths to reach ingress.

The job renderer assigns each application a hostname in this form:

```text
<project-slug>.<domain>
```

For example, the domain `apps.example.edu` and slug `demo` produce
`demo.apps.example.edu`.

A provider can use a wildcard DNS record and certificate or provision each
hostname separately. Per-application provisioning must finish before the
deployment is reported healthy.

## Request path

```text
browser or API client
        |
        | HTTPS
        v
public DNS/TLS service
        |
        | HTTP, original Host header preserved
        v
ingress host:80 (Traefik)
        |
        | healthy Nomad service only
        v
project worker:application port
```

Traefik selects the application route from the `Host` header. If the public
service replaces that header with an origin hostname or IP address, Traefik will
select the wrong route or no route.

Although the current role image opens ports 80 and 443, generated application
routers use the HTTP `web` entry point on port 80. Traefik does not currently
request or install public certificates. Do not send public HTTPS traffic
directly to port 443 unless you add and test a separate TLS configuration.

## Use Cloudflare Tunnel

Cloudflare Tunnel is enabled by default when you create the ingress host.
The Cloudflare account and token are unavoidable external inputs. Create a
direct current-user-owned mode-`0600` file containing exactly one non-whitespace
token line, then set its **path**:

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
or put it in a command argument. Verify the result through DNS and HTTPS, not
by echoing the file.

Configure Cloudflare to route the required hostnames through the tunnel to the
Traefik HTTP origin. This repository does not manage account-specific DNS or
tunnel configuration.

## Use another ingress service

To create the ingress host without cloudflared, set:

```bash
export ENABLE_CLOUDFLARED=false
```

Configure the external service to meet the required behavior above. Verify that
it forwards to the configured ingress address, preserves the original `Host`
header, and covers each new application hostname.

The platform health monitor currently calls its public check
`cloudflare_tunnel`, although the check makes ordinary HTTPS requests. With
another provider, the status key is a historical monitor label only; it does not
mean that cloudflared is running. `ENABLE_CLOUDFLARED=false` removes the
Cloudflare token requirement; the external provider still must preserve `Host`,
certificate validation, DNS, and health paths.

## Verify the public path

Check the platform health route through the public service:

```bash
PLATFORM_HOSTNAME=apps.example.edu
test "$(curl --fail --show-error --silent \
  "https://$PLATFORM_HOSTNAME/healthz")" = OK
```

The response body must be:

```text
OK
```

After deploying an application, check its configured health path through its
public hostname:

```bash
APPLICATION_HOSTNAME=demo.apps.example.edu
HEALTH_PATH=/health
test "$(curl --fail --show-error --silent --output /dev/null \
  --write-out '%{http_code}' "https://$APPLICATION_HOSTNAME$HEALTH_PATH")" = 200
```

A successful request verifies public DNS, certificate validation, forwarding,
host-based routing, the Nomad service route, and application health. A request
directly to the ingress IP does not verify the intended public path.

## Diagnose common failures

| Symptom | Check | Correction |
| --- | --- | --- |
| DNS name does not resolve | Query the exact platform or application hostname | Add the wildcard or per-host record in the public ingress service |
| Certificate name mismatch | Inspect the certificate's covered names | Issue a certificate that covers the exact hostname or wildcard |
| Traefik returns no matching route | Compare the forwarded `Host` header with `<slug>.<domain>` | Preserve the original host instead of replacing it with the origin address |
| `/healthz` works by ingress IP but not publicly | Test DNS, TLS, and the provider's origin route separately | Correct the public service's hostname or origin mapping |
| Platform route works but one application fails | Check Nomad allocation health and the application's configured health path | Restore a healthy allocation or correct the health path before publishing it |

See [`CONFIGURATION.md`](CONFIGURATION.md) for `domain` and `recoveryDomains`,
and [`CONTROL_PLANE_CONTRACT.md`](CONTROL_PLANE_CONTRACT.md) for application
route success criteria.
