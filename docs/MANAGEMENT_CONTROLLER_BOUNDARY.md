# Management-to-controller host and API contract

The future management application uses two processes. The browser-facing renderer never reaches the controller. A trusted authorization broker owns sessions, project ownership, quota, and controller idempotency intent.

## Process identities

| Process | Network | Durable state | Unix access |
| --- | --- | --- | --- |
| `management-web` | Listens on admin port 8080 only for ingress; all other IP traffic denied | Its own non-authoritative renderer state | Broker socket only |
| `management-broker` | No TCP/IP access | Sessions, users, project ownership, quota, audit, and reconciliation state | Controller project socket and its broker socket |

The controller project socket accepts only the exact `management-broker` UID and primary GID through Linux `SO_PEERCRED`. The browser renderer is not in `controller-api`, cannot traverse the controller runtime directory, and cannot read broker state. Authentication exchange and source lookup must use a separately reviewed typed Unix integration service; neither process may gain general provider/admin network access.

Both future executables are condition-gated below operator-owned release trees:

```text
<adminState>/management-broker-releases/current/bin/management-broker
<adminState>/management-web-releases/current/bin/management-web
```

The broker receives:

```text
CONTROLLER_PROJECT_SOCKET=/run/<namespace>-controller/project.sock
MANAGEMENT_BROKER_SOCKET=/run/<namespace>-management-broker/broker.sock
MANAGEMENT_STATE_DIRECTORY=<adminState>/management-broker
```

The renderer receives only `MANAGEMENT_BROKER_SOCKET` and its own state path.

## Controller socket capabilities

| Socket | Accepted peer | Routes |
| --- | --- | --- |
| `project.sock` | exact `management-broker` UID/GID | health and non-destructive project routes |
| `privileged.sock` | exact operator UID/GID | `/v1/admin/*`, application cascade delete, and storage delete |

Both sockets are mode `0660`, authenticate every connection before parsing HTTP, and apply global/per-peer connection limits and deadlines. HTTP input cannot upgrade a socket capability. The broker must enforce browser authentication, ownership, quota, and CSRF before project calls. Privileged routes must never be proxied to either management process.

The project socket excludes:

- `POST /v1/applications/{id}/delete`;
- `DELETE /v1/storage/{id}`; and
- every `/v1/admin/*` route.

## Verification

On admin, exercise deployed identities rather than root:

```sh
runuser -u management-broker -- curl --fail --silent \
  --unix-socket /run/<namespace>-controller/project.sock \
  http://localhost/v1/health

runuser -u management-web -- curl --fail --silent \
  --unix-socket /run/<namespace>-controller/project.sock \
  http://localhost/v1/health
# must fail

runuser -u management-broker -- curl --fail --silent \
  --unix-socket /run/<namespace>-controller/privileged.sock \
  http://localhost/v1/admin/status
# must fail
```

The admin VM test also checks process groups, socket ownership, denied routes, credential isolation, broker/web network policy, and that the renderer unit contains no controller socket path.
