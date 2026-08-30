# Management-to-controller host and API contract

The browser-facing management application is not implemented. This contract is
the boundary an implementation must consume; it does not grant browser-user
authorization by itself.

## Host identity

The process runs as the contract-defined `management-web` UID and primary GID.
Its systemd unit supplies:

```text
CONTROLLER_PROJECT_SOCKET=/run/<namespace>-controller/project.sock
MANAGEMENT_STATE_DIRECTORY=<adminState>/management-web
```

The unit may bind TCP port 8080 and accept traffic only from the configured
ingress host. Its cgroup denies all other IP traffic. It can use `AF_UNIX` to
open the project socket. The unit has no credentials and makes the controller,
operator, PKI, and secret trees inaccessible. A deployment that needs an
authentication exchange or source lookup must add reviewed destination-specific
network allowances; it must not remove the deny-by-default policy.

The executable is condition-gated at:

```text
<adminState>/management-web-releases/current/bin/management-web
```

The release tree is operator-owned and read-only to the service; management-web
owns only its state directory. Until that direct executable exists, systemd
skips the unit.

## Unix socket capabilities

| Socket | Filesystem group | Accepted `SO_PEERCRED` identity | Routes |
| --- | --- | --- | --- |
| `project.sock` | `controller-api` | exact `management-web` UID and primary GID | health and ordinary project routes |
| `privileged.sock` | `platform-admin` | exact operator UID and primary GID | `/v1/admin/*`, application cascade delete, storage delete |

The controller rejects missing, malformed, or non-allowlisted Linux peer
credentials before parsing HTTP. Each socket admits at most eight active
connections per allowed UID/GID in the Nix service. The limit is per process
identity and capacity is released when the connection closes. Socket group
membership alone is insufficient.

The project socket returns `404 NOT_FOUND` for privileged paths. An HTTP header,
browser role, request body, or route parameter cannot select the privileged
capability. A future management application must not receive the privileged
socket path or join `platform-admin`.

## Project API

`GET /v1/health` is the non-mutating readiness request:

```json
{"capability":"project","status":"ok"}
```

The project socket exposes the product routes in
[CONTROL_PLANE_CONTRACT.md](CONTROL_PLANE_CONTRACT.md), except:

- `POST /v1/applications/{id}/delete`;
- `DELETE /v1/storage/{id}`; and
- every `/v1/admin/*` route.

Browser authentication, project ownership, quota, CSRF protection, and
idempotency-intent persistence remain management-application obligations. The
controller authenticates only the local process identity.

## Privileged API

The privileged socket exposes only application cascade delete, storage delete,
and `/v1/admin/*` reads. It does not expose ordinary project creation,
deployment, environment, or operation-polling routes. Today it is an
operator-side API. Do not proxy it through the browser-facing process.

## Verification

On an admin host, use the deployed identities rather than root so `SO_PEERCRED`
is exercised:

```sh
runuser -u management-web -- curl --fail --silent \
  --unix-socket /run/<namespace>-controller/project.sock \
  http://localhost/v1/health

runuser -u management-web -- curl --fail --silent \
  --unix-socket /run/<namespace>-controller/privileged.sock \
  http://localhost/v1/admin/status
# must fail
```

The NixOS admin VM test also verifies socket ownership, both positive and denied
routes, credential-file denial, and the management unit's network and filesystem
sandbox properties.
