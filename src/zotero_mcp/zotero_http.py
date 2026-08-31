"""Central HTTP routing for Zotero MCP."""

from __future__ import annotations

from enum import Enum
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

import requests

NORMAL_PROXY_PORT = 17892
PROXY_REQUIRED_PORT = 17893
DIRECT_PROXIES = {"http": "", "https": "", "all": ""}
WILEY_SUFFIXES = (
    "wiley.com",
    "wiley-vch.de",
    "wiley-vch.com",
    "wileyplus.com",
)
WILEY_EXACT_DOMAINS = {
    "wiley.scienceconnect.io",
    "wiley.grapeshot.co.uk",
    "wiley.met.vgwort.de",
}


class RouteType(str, Enum):
    """Transport intent declared by Zotero MCP callers."""

    LOCAL = "local"
    NORMAL = "normal"
    PROXY_REQUIRED = "proxy_required"


class RouteUnavailableError(requests.ConnectionError):
    """The configured route cannot be constructed."""


@lru_cache(maxsize=1)
def wsl_gateway_ip() -> str | None:
    """Return the current WSL default gateway without shelling out."""

    route_path = Path("/proc/net/route")
    if not route_path.exists():
        return None
    for line in route_path.read_text(encoding="ascii").splitlines()[1:]:
        fields = line.split()
        if len(fields) < 3 or fields[1] != "00000000":
            continue
        try:
            raw = bytes.fromhex(fields[2])
        except ValueError:
            continue
        return ".".join(str(part) for part in reversed(raw))
    return None


def proxy_url(route: RouteType) -> str:
    """Return the Windows Mihomo endpoint dedicated to one external route."""

    gateway = wsl_gateway_ip()
    if not gateway:
        raise RouteUnavailableError(
            "Unable to determine the WSL gateway for Zotero MCP HTTP routing"
        )
    port = (
        PROXY_REQUIRED_PORT
        if route is RouteType.PROXY_REQUIRED
        else NORMAL_PROXY_PORT
    )
    return f"http://{gateway}:{port}"


def is_wiley_domain(host: str) -> bool:
    value = host.casefold().strip(".")
    return value in WILEY_EXACT_DOMAINS or any(
        value == suffix or value.endswith(f".{suffix}") for suffix in WILEY_SUFFIXES
    )


def is_wiley_url(url: str) -> bool:
    return is_wiley_domain(urlsplit(url).hostname or "")


def external_route(url: str) -> RouteType:
    """Classify an external URL, upgrading Wiley to fail-closed proxy routing."""

    return RouteType.PROXY_REQUIRED if is_wiley_url(url) else RouteType.NORMAL


def route_proxies(route: RouteType) -> dict[str, str]:
    if route is RouteType.LOCAL:
        return {}
    selected = proxy_url(route)
    return {"http": selected, "https": selected}


def routed_session(
    route: RouteType,
    session: requests.Session | None = None,
) -> requests.Session:
    """Configure a session for one route without inheriting shell proxy variables."""

    selected = session if session is not None else requests.Session()
    selected.trust_env = False
    selected.proxies = route_proxies(route)
    return selected


def session_request(
    session: requests.Session,
    method: str,
    url: str,
    *,
    route: RouteType,
    **kwargs: object,
) -> requests.Response:
    """Make a routed request while preserving a caller-owned session."""

    session.trust_env = False
    kwargs["proxies"] = route_proxies(route)
    return session.request(method, url, **kwargs)


def request(
    method: str,
    url: str,
    *,
    route: RouteType,
    **kwargs: object,
) -> requests.Response:
    """Make a one-shot routed request."""

    kwargs["proxies"] = (
        dict(DIRECT_PROXIES) if route is RouteType.LOCAL else route_proxies(route)
    )
    return requests.request(method, url, **kwargs)


def get(url: str, *, route: RouteType, **kwargs: object) -> requests.Response:
    return request("GET", url, route=route, **kwargs)


def post(url: str, *, route: RouteType, **kwargs: object) -> requests.Response:
    return request("POST", url, route=route, **kwargs)


def put(url: str, *, route: RouteType, **kwargs: object) -> requests.Response:
    return request("PUT", url, route=route, **kwargs)
