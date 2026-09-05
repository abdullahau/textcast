"""Fetching a URL an article or a picture points at, without trusting it.

Both `service.fetch` and `pictures._download` run inside the ingest request,
and the address is whatever a newsletter or a web page put there — a link, an
`<img src>`, or a redirect target none of them control. Left alone, that is a
standing invitation for a crafted address to make this box fetch from itself
or from whatever else shares its network. Every hop, including a redirect's,
is resolved and checked before it is connected to, and `get` pins the
connection to the address just checked — a hostname is resolved once, not
again by `requests` a moment later, which is what stops a short-TTL DNS
answer changing between the two.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import ParseResult, urljoin, urlparse

import requests
from requests.adapters import HTTPAdapter

#: A chain longer than this is not a publication redirecting once to its own
#: CDN; it is not worth following further.
MAX_REDIRECTS = 5


class UnsafeURL(RuntimeError):
    pass


def _is_public(ip: str) -> bool:
    # `is_global` is the one property IANA's special-purpose registry is
    # checked against as a whole, including 100.64.0.0/10 (carrier-grade NAT,
    # and the range Tailscale hands its own nodes) — a range that is not
    # `is_private` and so slipped past a check built from the other flags.
    return ipaddress.ip_address(ip).is_global


def _http_url(url: str) -> ParseResult:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise UnsafeURL(f"refused {url}: not http(s)")
    if not parsed.hostname:
        raise UnsafeURL(f"refused {url}: no host")
    return parsed


def _resolve(hostname: str, url: str) -> list[str]:
    """Every address ``hostname`` resolves to, each checked public."""
    try:
        infos = socket.getaddrinfo(hostname, None)
    except OSError as exc:
        raise UnsafeURL(f"refused {url}: {exc}") from exc
    addrs = [info[4][0] for info in infos]
    for ip in addrs:
        if not _is_public(ip):
            raise UnsafeURL(f"refused {url}: {ip} is not a public address")
    return addrs


def check(url: str) -> None:
    """Raise `UnsafeURL` unless every address this host resolves to is public."""
    parsed = _http_url(url)
    _resolve(parsed.hostname, url)


class _PinnedAdapter(HTTPAdapter):
    """Connects to the address `_resolve` already checked, not to whatever
    ``hostname`` re-resolves to when urllib3 opens the socket. TLS still
    verifies the certificate against ``hostname`` — only the address dialed
    is pinned, not what a certificate is checked against.
    """

    def __init__(self, hostname: str, ip: str, https: bool) -> None:
        self._hostname = hostname
        self._ip = ip
        self._https = https
        super().__init__()

    def build_connection_pool_key_attributes(self, request, verify, cert=None):
        host_params, pool_kwargs = super().build_connection_pool_key_attributes(
            request, verify, cert
        )
        if host_params.get("host") == self._hostname:
            host_params["host"] = self._ip
            if self._https:
                # Unset on a plain HTTPConnection: only HTTPSConnection takes
                # these, so they are added only for the scheme that uses them.
                pool_kwargs["assert_hostname"] = self._hostname
                pool_kwargs["server_hostname"] = self._hostname
        return host_params, pool_kwargs

    def send(self, request, **kwargs):
        # http.client's Host header defaults to the *connection's* host,
        # which is now the pinned IP -- an edge doing name-based routing
        # (confirmed against a real one: Wikipedia's, 400 "invalid request")
        # needs the real hostname there, not the address it happens to
        # answer on.
        request.headers.setdefault("Host", self._hostname)
        return super().send(request, **kwargs)


def get(url: str, **kwargs) -> requests.Response:
    """`requests.get`, with every hop — including a redirect's — resolved,
    checked, and pinned to the address `_resolve` just validated."""
    kwargs["allow_redirects"] = False
    session = requests.Session()
    for _ in range(MAX_REDIRECTS + 1):
        parsed = _http_url(url)
        ip = _resolve(parsed.hostname, url)[0]
        https = parsed.scheme == "https"
        session.mount(
            f"{parsed.scheme}://", _PinnedAdapter(parsed.hostname, ip, https)
        )
        response = session.get(url, **kwargs)
        if not response.is_redirect:
            return response
        location = response.headers.get("Location")
        response.close()
        if not location:
            raise UnsafeURL(f"refused {url}: redirect with no location")
        url = urljoin(url, location)
    raise UnsafeURL(f"refused {url}: too many redirects")
