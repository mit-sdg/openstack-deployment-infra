"""Bounded REST reads with a shared Keystone plugin and isolated HTTP pools."""

from __future__ import annotations

import json
import logging
import re
import socket
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Any, BinaryIO
from urllib.parse import quote
from uuid import UUID

import requests
from keystoneauth1 import session as ks_session
from keystoneauth1.identity import v3
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.util.connection import allowed_gai_family

from .provider_protocol import (
    FILTERS,
    MAX_RESPONSE,
    MAX_ROWS,
    RESOURCES,
    ProviderError,
    endpoint_url,
    remaining,
    text,
    validate,
)


def credential(value: object) -> str:
    if not isinstance(value, str) or not value or len(value.encode()) > 65_536 or "\x00" in value:
        raise ProviderError("configuration")
    return value


_transport_context = threading.local()
_dns_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="provider-dns")
_dns_slots = threading.BoundedSemaphore(8)


def _resolve(host: str, port: int, deadline: float) -> list[Any]:
    # libc name resolution is not interruptible by socket timeouts. Limit both
    # the caller's wait and outstanding resolver work; a timed-out resolver can
    # only return addresses, never send a later authenticated request.
    if not _dns_slots.acquire(timeout=remaining(deadline)):
        raise ProviderError("timeout")
    try:
        future = _dns_pool.submit(
            socket.getaddrinfo, host, port, allowed_gai_family(), socket.SOCK_STREAM
        )
    except Exception:
        _dns_slots.release()
        raise
    future.add_done_callback(lambda _result: _dns_slots.release())
    try:
        return future.result(timeout=remaining(deadline))
    except TimeoutError:
        future.cancel()
        raise ProviderError("timeout") from None


def _interrupt(connection: Any) -> None:
    # shutdown wakes a blocked status/header/body read; close alone may leave a
    # different thread waiting on its file object's descriptor. The connection
    # is discarded on the resulting request failure.
    connected = connection.sock
    if connected is not None:
        try:
            connected.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


class DeadlineConnection:
    def request(self, *args: Any, **kwargs: Any) -> None:
        deadline = _transport_context.deadline
        timer = threading.Timer(max(0, deadline - time.monotonic()), _interrupt, (self,))
        timer.daemon = True
        _transport_context.timers.append(timer)
        timer.start()
        super().request(*args, **kwargs)  # type: ignore[misc]

    def _new_conn(self) -> socket.socket:
        connection: Any = self
        deadline = _transport_context.deadline
        addresses = _resolve(connection._dns_host, connection.port, deadline)
        for family, kind, protocol, _canonical, address in addresses:
            connected = socket.socket(family, kind, protocol)
            try:
                seconds = remaining(deadline)
                network_timeout = connection.timeout
                connected.settimeout(
                    min(seconds, network_timeout)
                    if isinstance(network_timeout, (int, float))
                    else seconds
                )
                for option in connection.socket_options or ():
                    connected.setsockopt(*option)
                if connection.source_address:
                    connected.bind(connection.source_address)
                connected.connect(address)
                # TLS inherits this timeout for its handshake. Status/header
                # reads also have the per-connection absolute watchdog.
                connected.settimeout(remaining(deadline))
                return connected
            except OSError:
                connected.close()
            except BaseException:
                connected.close()
                raise
        raise ProviderError()

    def connect(self) -> None:
        super().connect()  # type: ignore[misc]
        # DNS resolution precedes socket construction. If it returns after the
        # deadline, never send credentials or a service request on that socket.
        try:
            remaining(_transport_context.deadline)
        except ProviderError:
            self.close()  # type: ignore[attr-defined]
            raise


class DeadlineHTTPConnection(DeadlineConnection, HTTPConnection):
    pass


class DeadlineHTTPSConnection(DeadlineConnection, HTTPSConnection):
    pass


class DeadlineHTTPPool(HTTPConnectionPool):
    ConnectionCls = DeadlineHTTPConnection


class DeadlineHTTPSPool(HTTPSConnectionPool):
    ConnectionCls = DeadlineHTTPSConnection


class DeadlineAdapter(HTTPAdapter):
    def init_poolmanager(
        self, connections: int, maxsize: int, block: bool = False, **pool_kwargs: Any
    ) -> None:
        super().init_poolmanager(connections, maxsize, block=block, **pool_kwargs)
        self.poolmanager.pool_classes_by_scheme = {
            "http": DeadlineHTTPPool,
            "https": DeadlineHTTPSPool,
        }


class BoundedHTTP(requests.Session):
    """Bound authentication, catalog discovery, and service responses alike.

    Each instance belongs to one worker. No cookie/proxy/environment state is
    shared. read1 returns available bytes, allowing a deadline check even when
    a peer trickles a body. No redirects or transport retries are permitted.
    """

    def __init__(self, *, allow_http: bool = False) -> None:
        super().__init__()
        self.trust_env = False
        self.headers["Accept-Encoding"] = "identity"
        self.allow_http = allow_http
        self.deadline = 0.0
        self.limit = MAX_RESPONSE
        self.stream_sink: BinaryIO | None = None
        for scheme in ("http://", "https://"):
            self.mount(scheme, DeadlineAdapter(pool_connections=8, pool_maxsize=1, max_retries=0))

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:  # type: ignore[override]
        endpoint_url(url.split("?", 1)[0], allow_http=self.allow_http)
        # Connecting is quick or broken; the response may take the caller's
        # whole remaining budget (console output goes over RPC to the host).
        seconds = remaining(self.deadline)
        kwargs.update(timeout=(min(5.0, seconds), seconds), allow_redirects=False, stream=True)
        _transport_context.deadline = self.deadline
        _transport_context.timers = []
        response = None
        try:
            response = super().request(method, url, **kwargs)
            if self.stream_sink is not None and response.status_code != 200:
                response.raw.close()
                response._content = b""
                response._content_consumed = True  # type: ignore[attr-defined]
                return response
            if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                raise ProviderError("malformed")
            length = response.headers.get("Content-Length")
            if length is not None and int(length) > self.limit:
                raise ProviderError("size")
            body = bytearray()
            received = 0
            while True:
                seconds = remaining(self.deadline)
                connection = response.raw._connection
                if connection is not None and connection.sock is not None:
                    connection.sock.settimeout(seconds)
                chunk = response.raw.read1(
                    min(65_536, self.limit + 1 - received), decode_content=True
                )
                if not chunk:
                    break
                received += len(chunk)
                if received > self.limit:
                    raise ProviderError("size")
                if self.stream_sink is None:
                    body.extend(chunk)
                elif self.stream_sink.write(chunk) != len(chunk):
                    raise ProviderError()
            remaining(self.deadline)
            response._content = bytes(body)
            response._content_consumed = True  # type: ignore[attr-defined]
            return response
        finally:
            for timer in _transport_context.timers:
                timer.cancel()
            if response is not None:
                response.close()
            self.cookies.clear()


class API:
    """Eight reusable worker sessions share one refreshable, project-scoped token.

    Callers provide action data, never URLs, credentials, headers, or methods.
    A bounded lock protects auth refresh/catalog selection; worker HTTP sessions
    are exclusively leased so requests.Session is never used concurrently.
    """

    def __init__(
        self,
        environment: Mapping[str, str],
        *,
        allow_http: bool = False,
        check_credentials: Callable[[], None] | None = None,
    ) -> None:
        self.check_credentials = check_credentials
        self.environment = dict(environment)
        self.allow_http_glance = (
            environment.get("PLATFORM_ALLOW_HTTP_GLANCE")
            == "I_UNDERSTAND_GLANCE_CREDENTIALS_USE_HTTP"
        )
        self.allow_http = allow_http
        auth_url = endpoint_url(text(environment.get("OS_AUTH_URL")), allow_http=allow_http)
        kind = environment.get("OS_AUTH_TYPE", "password")
        self.auth: v3.Auth
        if kind in {"password", "v3password"}:
            self.auth = v3.Password(
                auth_url=auth_url,
                username=text(environment.get("OS_USERNAME")),
                password=credential(environment.get("OS_PASSWORD")),
                user_domain_id=environment.get("OS_USER_DOMAIN_ID"),
                user_domain_name=environment.get("OS_USER_DOMAIN_NAME", "Default"),
                project_id=environment.get("OS_PROJECT_ID"),
                project_name=environment.get("OS_PROJECT_NAME"),
                project_domain_id=environment.get("OS_PROJECT_DOMAIN_ID"),
                project_domain_name=environment.get("OS_PROJECT_DOMAIN_NAME", "Default"),
            )
        elif kind in {"v3applicationcredential", "applicationcredential"}:
            self.auth = v3.ApplicationCredential(
                auth_url=auth_url,
                application_credential_id=text(environment.get("OS_APPLICATION_CREDENTIAL_ID")),
                application_credential_secret=credential(
                    environment.get("OS_APPLICATION_CREDENTIAL_SECRET")
                ),
            )
        else:
            raise ProviderError("configuration")
        if environment.get("OS_INSECURE", "false").lower() not in {"false", "0", ""}:
            raise ProviderError("configuration")
        self.interface = environment.get("OS_INTERFACE", "public")
        if self.interface not in {"public", "internal", "admin"}:
            raise ProviderError("configuration")
        self.region = environment.get("OS_REGION_NAME")
        self.verify: bool | str = environment.get("OS_CACERT") or True
        self.auth_lock = threading.Lock()
        self.condition = threading.Condition()
        self.compute_versions: dict[str, str] = {}
        self.flavor_names: dict[str, str] = {}
        # Service URLs come from the catalog, as with the OpenStack CLI, and
        # may be HTTP; the auth URL above must be HTTPS outside tests.
        self.available = [BoundedHTTP(allow_http=True) for _ in range(8)]
        # Keystone may include provider bodies in DEBUG messages. Disable its
        # HTTP loggers even if a caller configures the root logger for debugging.
        logging.getLogger("keystoneauth").disabled = True
        logging.getLogger("keystoneauth.session").disabled = True

    @contextmanager
    def lease(self, deadline: float) -> Iterator[tuple[BoundedHTTP, ks_session.Session]]:
        with self.condition:
            while not self.available:
                self.condition.wait(remaining(deadline))
            http = self.available.pop()
        http.deadline = deadline
        http.limit = MAX_RESPONSE
        session = ks_session.Session(
            auth=self.auth, session=http, verify=self.verify, redirect=False
        )
        try:
            yield http, session
        finally:
            http.stream_sink = None
            http.limit = MAX_RESPONSE
            with self.condition:
                self.available.append(http)
                self.condition.notify()

    def credentials(
        self, session: ks_session.Session, deadline: float, service: str | None
    ) -> tuple[Any, str | None]:
        if not self.auth_lock.acquire(timeout=remaining(deadline)):
            raise ProviderError("timeout")
        try:
            access = self.auth.get_access(session)
            remaining(deadline)
            if access.will_expire_soon(0):
                raise ProviderError("identity")
            project = access.project_id
            if not project or (
                self.environment.get("OS_PROJECT_ID")
                and project.replace("-", "") != self.environment["OS_PROJECT_ID"].replace("-", "")
            ):
                raise ProviderError("identity")
            configured_name = self.environment.get("OS_PROJECT_NAME")
            if configured_name and access.project_name != configured_name:
                raise ProviderError("identity")
            endpoint = None
            if service:
                matches = access.service_catalog.get_endpoints(
                    service_type=service, interface=self.interface, region_name=self.region
                )
                urls = {
                    item["url"] for rows in matches.values() for item in rows if item is not None
                }
                if len(urls) != 1:
                    raise ProviderError("configuration")
                endpoint = endpoint_url(urls.pop(), allow_http=True)
                if service in {"image", "network"}:
                    version = "v2" if service == "image" else "v2.0"
                    if not endpoint.endswith("/" + version):
                        endpoint += "/" + version
                if service == "compute" and not re.search(r"/v2(?:\.1)?(?:/[^/]+)?$", endpoint):
                    raise ProviderError("configuration")
                if service == "volumev3" and not re.search(r"/v3/[^/]+$", endpoint):
                    raise ProviderError("configuration")
            return access, endpoint
        finally:
            self.auth_lock.release()

    def request(
        self,
        session: ks_session.Session,
        deadline: float,
        service: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        body: object = None,
    ) -> Any:
        for attempt in range(2):
            access, endpoint = self.credentials(session, deadline, service)
            assert endpoint is not None
            headers = {"X-Auth-Token": access.auth_token}
            version = None
            if service == "compute":
                version = self.compute_version(session, deadline, access, endpoint)
                if version is None:
                    if attempt == 0:
                        self.invalidate(access, deadline)
                        continue
                    raise ProviderError("http", 401)
                # Nova before 2.27 reads only the legacy header and silently
                # serves 2.1 for anything else, so send both.
                headers["X-OpenStack-Nova-API-Version"] = version
                headers["OpenStack-API-Version"] = f"compute {version}"
            response = session.request(
                endpoint + "/" + path,
                "POST" if body is not None else "GET",
                authenticated=False,
                headers=headers,
                params=params,
                json=body,
                raise_exc=False,
                redirect=False,
                connect_retries=0,
                status_code_retries=0,
            )
            if response.status_code == 401 and attempt == 0:
                self.invalidate(access, deadline)
                continue
            if response.status_code != 200:
                raise ProviderError("http", response.status_code)
            if version is not None and (
                response.headers.get("X-OpenStack-Nova-API-Version") != version
            ):
                raise ProviderError("malformed")
            return response.json()
        raise ProviderError()

    def name_flavors(
        self, session: ks_session.Session, deadline: float, servers: list[dict[str, Any]]
    ) -> None:
        """Add original_name to each server flavor, as Nova 2.47+ and the CLI report it.

        Below 2.47 a server's flavor is only its ID. Flavor IDs are immutable,
        so names are cached; a deleted flavor keeps its ID without a name.
        """
        for server in servers:
            flavor = server.get("flavor")
            if not isinstance(flavor, dict) or "original_name" in flavor:
                continue
            identifier = flavor.get("id")
            if not isinstance(identifier, str) or not identifier:
                raise ProviderError("malformed")
            with self.condition:
                name = self.flavor_names.get(identifier)
            if name is None:
                try:
                    document = self.request(
                        session, deadline, "compute", "flavors/" + quote(identifier, safe="")
                    )
                except ProviderError as error:
                    if error.status == 404:
                        continue
                    raise
                name = text(document["flavor"]["name"])
                with self.condition:
                    self.flavor_names[identifier] = name
            server["flavor"] = {**flavor, "original_name": name}

    def compute_version(
        self, session: ks_session.Session, deadline: float, access: Any, endpoint: str
    ) -> str | None:
        """Negotiate one Nova microversion from 2.3 up to 2.46, or None for a rejected token.

        2.3 adds delete_on_termination to attached volumes; 2.47 replaces the
        server flavor ID with an embedded description. Within that range the
        server, flavor, keypair, limit and quota reads keep one shape.
        """
        root = (
            re.sub(r"/[^/]+$", "", endpoint)
            if re.search(r"/v2(?:\.1)?/[^/]+$", endpoint)
            else endpoint
        )
        with self.condition:
            cached = self.compute_versions.get(root)
        if cached is not None:
            return cached
        response = session.request(
            root + "/",
            "GET",
            authenticated=False,
            headers={"X-Auth-Token": access.auth_token},
            raise_exc=False,
            redirect=False,
            connect_retries=0,
            status_code_retries=0,
        )
        if response.status_code == 401:
            return None
        if response.status_code != 200:
            raise ProviderError("http", response.status_code)
        try:
            document = response.json()["version"]
            newest = tuple(int(part) for part in document["version"].split("."))
            oldest = tuple(int(part) for part in document["min_version"].split("."))
        except (KeyError, TypeError, ValueError, AttributeError):
            raise ProviderError("configuration") from None
        chosen = min(newest, (2, 46))
        if len(chosen) != 2 or chosen < (2, 3) or oldest > chosen:
            raise ProviderError("configuration")
        version = f"{chosen[0]}.{chosen[1]}"
        with self.condition:
            self.compute_versions[root] = version
        return version

    def invalidate(self, access: Any, deadline: float) -> None:
        if not self.auth_lock.acquire(timeout=remaining(deadline)):
            raise ProviderError("timeout")
        try:
            if self.auth.auth_ref is access:
                self.auth.invalidate()
        finally:
            self.auth_lock.release()

    def download_image(
        self,
        reference: str,
        stream: BinaryIO,
        *,
        deadline: float,
        maximum_bytes: int = 16 * 1024**3,
    ) -> None:
        """Stream a setup image into its caller-owned file, without a daemon path.

        This local API is used only by setup, which already holds credentials.
        Ordinary socket responses and authentication keep their 1 MiB limit.
        A failed/partial download is never retried; setup discards its temp file.
        """
        if type(maximum_bytes) is not int or not 0 < maximum_bytes <= 16 * 1024**3:
            raise ProviderError("invalid")
        try:
            identifier = str(UUID(reference))
            with self.lease(deadline) as (http, session):
                for attempt in range(2):
                    http.stream_sink = None
                    http.limit = MAX_RESPONSE
                    access, endpoint = self.credentials(session, deadline, "image")
                    assert endpoint is not None
                    http.stream_sink = stream
                    http.limit = maximum_bytes
                    response = session.request(
                        endpoint + "/images/" + identifier + "/file",
                        "GET",
                        authenticated=False,
                        headers={"X-Auth-Token": access.auth_token},
                        raise_exc=False,
                        redirect=False,
                        connect_retries=0,
                        status_code_retries=0,
                    )
                    if response.status_code == 401 and attempt == 0:
                        self.invalidate(access, deadline)
                        continue
                    if response.status_code != 200:
                        raise ProviderError("http", response.status_code)
                    return
        except ProviderError:
            raise
        except Exception:
            raise ProviderError() from None

    def inventory(
        self, session: ks_session.Session, deadline: float, kind: str, filters: Mapping[str, str]
    ) -> list[dict[str, Any]]:
        service, path, envelope, _ = RESOURCES[kind]
        if kind in {"server", "flavor", "volume"}:
            path += "/detail"
        params = dict(filters)
        # A Nova server record can carry up to 64 KiB of base64 user data, so
        # server pages stay small enough for one bounded response.
        params["limit"] = str(10 if kind == "server" else MAX_ROWS + 1)
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        total = 0
        while True:
            document = self.request(session, deadline, service, path, params=params)
            batch = document.get(envelope)
            if not isinstance(batch, list) or any(not isinstance(row, dict) for row in batch):
                raise ProviderError("malformed")
            if kind == "keypair":
                batch = [row["keypair"] for row in batch]
            if kind == "server":
                for row in batch:
                    row.pop(_SERVER_USER_DATA, None)
            total += len(json.dumps(batch).encode())
            if len(rows) + len(batch) > MAX_ROWS or total > MAX_RESPONSE:
                raise ProviderError("size")
            for row in batch:
                identifier = str(row.get("id", row.get("name", "")))
                if not identifier or identifier in seen:
                    raise ProviderError("malformed")
                seen.add(identifier)
            rows.extend(batch)
            links = document.get(f"{envelope}_links", document.get("links", []))
            more = document.get("next") or (
                isinstance(links, list)
                and any(isinstance(link, dict) and link.get("rel") == "next" for link in links)
            )
            if not more:
                return rows
            # Never follow provider URLs with a token. Rebuild the next request
            # against the pinned endpoint and original filters using its marker.
            if not batch:
                raise ProviderError("malformed")
            params["marker"] = str(batch[-1]["id"])

    def show(
        self, session: ks_session.Session, deadline: float, kind: str, reference: str
    ) -> dict[str, Any]:
        service, path, _, envelope = RESOURCES[kind]
        # Neutron/Nova/Cinder resource paths require UUIDs. Resolve names by
        # exact inventory matching; flavors use opaque IDs and keypairs names.
        try:
            UUID(reference)
            uuid_reference = True
        except ValueError:
            uuid_reference = False
        if kind not in {"flavor", "keypair"} and not uuid_reference:
            return self.named(session, deadline, kind, reference)
        try:
            document = self.request(
                session, deadline, service, path + "/" + quote(reference, safe="")
            )
        except ProviderError as error:
            if error.status != 404 or kind != "flavor":
                raise
            return self.named(session, deadline, kind, reference)
        result = document.get(envelope) if envelope else document
        if not isinstance(result, dict):
            raise ProviderError("malformed")
        return result

    def named(
        self, session: ks_session.Session, deadline: float, kind: str, reference: str
    ) -> dict[str, Any]:
        # Nova's name filter is a regular expression, so keep the exact match.
        filters = {"name": reference} if "name" in FILTERS.get(kind, ()) else {}
        matches = [
            row
            for row in self.inventory(session, deadline, kind, filters)
            if row.get("name") == reference
        ]
        if len(matches) != 1:
            raise ProviderError("identity", 404)
        service, path, _, envelope = RESOURCES[kind]
        document = self.request(
            session, deadline, service, path + "/" + quote(text(matches[0]["id"]), safe="")
        )
        result = document.get(envelope) if envelope else document
        if not isinstance(result, dict):
            raise ProviderError("malformed")
        return result

    def execute(self, action: str, params: dict[str, Any], deadline: float) -> Any:
        validate(action, params)
        try:
            if self.check_credentials is not None:
                self.check_credentials()
            with self.lease(deadline) as (_, session):
                if action == "identity":
                    access, _ = self.credentials(session, deadline, None)
                    return {"project_id": access.project_id, "name": access.project_name}
                if action == "image.catalog":
                    access, _ = self.credentials(session, deadline, "image")
                    return {
                        "endpoints": [
                            item
                            for rows in access.service_catalog.get_endpoints(
                                service_type="image"
                            ).values()
                            for item in rows
                        ]
                    }
                if action == "image.usage":
                    # Setup's quota read has always required an explicit
                    # acknowledgement before sending a token to HTTP Glance.
                    _, endpoint = self.credentials(session, deadline, "image")
                    if (
                        endpoint is None
                        or endpoint.startswith("http://")
                        and not (self.allow_http or self.allow_http_glance)
                    ):
                        raise ProviderError("configuration")
                    return self.request(session, deadline, "image", "info/usage")
                if action in {"console", "attachments"}:
                    try:
                        identifier = str(UUID(params["reference"]))
                    except ValueError:
                        identifier = text(
                            self.show(session, deadline, "server", params["reference"])["id"]
                        )
                    identifier = quote(identifier, safe="")
                    if action == "console":
                        result = self.request(
                            session,
                            deadline,
                            "compute",
                            f"servers/{identifier}/action",
                            body={
                                "os-getConsoleOutput": {}
                                if params["lines"] is None
                                else {"length": params["lines"]}
                            },
                        )
                        return text_output(result.get("output"))
                    result = self.request(
                        session, deadline, "compute", f"servers/{identifier}/os-volume_attachments"
                    )
                    return result["volumeAttachments"]
                if action == "limits":
                    result = {}
                    for service in ("compute", "volumev3"):
                        result.update(
                            self.request(session, deadline, service, "limits")["limits"]["absolute"]
                        )
                    names = {
                        re.sub(
                            r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", "_", key
                        ).lower(): (value)
                        for key, value in result.items()
                    }
                    # The CLI also reports these counters under the short
                    # names setup's quota fallback reads.
                    for short in ("instances_used", "security_groups_used"):
                        if f"total_{short}" in names:
                            names[short] = names[f"total_{short}"]
                    return [{"Name": key, "Value": value} for key, value in names.items()]
                if action == "quota":
                    access, _ = self.credentials(session, deadline, None)
                    project = quote(text(access.project_id), safe="")
                    result = {}
                    if not params.get("network"):
                        usage = bool(params.get("usage"))
                        # Nova has a /detail route; Cinder takes ?usage=True.
                        result.update(
                            self.request(
                                session,
                                deadline,
                                "compute",
                                f"os-quota-sets/{project}" + ("/detail" if usage else ""),
                            )["quota_set"]
                        )
                        result.update(
                            self.request(
                                session,
                                deadline,
                                "volumev3",
                                f"os-quota-sets/{project}",
                                params={"usage": "True"} if usage else None,
                            )["quota_set"]
                        )
                    path = f"quotas/{project}" + ("/details" if params.get("usage") else "")
                    network = self.request(session, deadline, "network", path)["quota"]
                    aliases = {
                        "floatingip": "floating_ips",
                        "port": "ports",
                        "security_group": "security_groups",
                        "security_group_rule": "security_group_rules",
                    }
                    result.update({aliases.get(key, key): value for key, value in network.items()})
                    result.pop("id", None)
                    return (
                        result
                        if params.get("usage")
                        else [{"Resource": key, "Limit": value} for key, value in result.items()]
                    )
                kind, verb = action.split(".")
                rows = (
                    self.inventory(session, deadline, kind, params)
                    if verb == "list"
                    else [self.show(session, deadline, kind, params["reference"])]
                )
                projected = [project_resource(kind, row) for row in rows]
                if kind == "server":
                    self.name_flavors(session, deadline, projected)
                return projected if verb == "list" else projected[0]
        except ProviderError:
            raise
        except Exception:
            raise ProviderError() from None


def text_output(value: object) -> str:
    if not isinstance(value, str):
        raise ProviderError("malformed")
    return value


_SERVER_USER_DATA = "OS-EXT-SRV-ATTR:user_data"


def project_resource(kind: str, row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    if kind == "image":
        # Glance properties are top-level. Its documented base attributes are
        # separate from metadata; do not infer missing ownership.
        standard = {
            "id",
            "name",
            "status",
            "created_at",
            "updated_at",
            "owner",
            "visibility",
            "protected",
            "tags",
            "checksum",
            "size",
            "virtual_size",
            "container_format",
            "disk_format",
            "min_disk",
            "min_ram",
            "self",
            "file",
            "schema",
            "os_hash_algo",
            "os_hash_value",
        }
        # As in the CLI, custom properties appear once, under properties.
        result = {key: value for key, value in row.items() if key in standard}
        result["properties"] = {key: value for key, value in row.items() if key not in standard}
    elif kind == "server":
        # The CLI reports metadata as properties. Boot user data is secret
        # bootstrap material that no read needs.
        result["properties"] = result.pop("metadata", None)
        result.pop(_SERVER_USER_DATA, None)
        if "os-extended-volumes:volumes_attached" in row:
            result["volumes_attached"] = row["os-extended-volumes:volumes_attached"]
        addresses = row.get("addresses")
        if isinstance(addresses, dict):
            result["addresses"] = {
                key: [item.get("addr") for item in values] for key, values in addresses.items()
            }
    elif kind in {
        "port",
        "network",
        "subnet",
        "router",
        "floating_ip",
        "security_group",
        "security_group_rule",
    }:
        if "project_id" not in row and "tenant_id" in row:
            result["project_id"] = row["tenant_id"]
        # The CLI reports Neutron ownership once, as project_id.
        result.pop("tenant_id", None)
    if kind == "port" and "security_groups" in row:
        result["security_group_ids"] = result.pop("security_groups")
    if kind == "volume":
        result["type"] = row.get("volume_type")
    if kind == "security_group_rule":
        result["remote_group_id"] = row.get("remote_group_id")
    return result
