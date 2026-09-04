"""Fetching a URL an article or a picture points at, without trusting it.

Both `service.fetch` and `pictures._download` run inside the ingest request,
and the address is whatever a newsletter or a web page put there — a link, an
`<img src>`, or a redirect target none of them control. Left alone, that is a
standing invitation for a crafted address to make this box fetch from itself
or from whatever else shares its network. Every hop, including a redirect's,
is resolved and checked before it is connected to.

DNS can still change between the check here and the connection `requests`
makes a moment later — closing that gap needs a transport pinned to the
address already resolved, which is more machinery than this app carries
elsewhere. What this stops is the address simply *being* internal, which is
what a crafted link or an open redirect on a third party's server would try.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urljoin, urlparse

import requests

#: A chain longer than this is not a publication redirecting once to its own
#: CDN; it is not worth following further.
MAX_REDIRECTS = 5


class UnsafeURL(RuntimeError):
    pass


def _is_public(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    return not (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    )


def check(url: str) -> None:
    """Raise `UnsafeURL` unless every address this host resolves to is public."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise UnsafeURL(f"refused {url}: not http(s)")
    if not parsed.hostname:
        raise UnsafeURL(f"refused {url}: no host")
    try:
        infos = socket.getaddrinfo(parsed.hostname, None)
    except OSError as exc:
        raise UnsafeURL(f"refused {url}: {exc}") from exc
    for info in infos:
        ip = info[4][0]
        if not _is_public(ip):
            raise UnsafeURL(f"refused {url}: {ip} is not a public address")


def get(url: str, **kwargs) -> requests.Response:
    """`requests.get`, with every hop — including a redirect's — checked first."""
    kwargs["allow_redirects"] = False
    for _ in range(MAX_REDIRECTS + 1):
        check(url)
        response = requests.get(url, **kwargs)
        if not response.is_redirect:
            return response
        location = response.headers.get("Location")
        response.close()
        if not location:
            raise UnsafeURL(f"refused {url}: redirect with no location")
        url = urljoin(url, location)
    raise UnsafeURL(f"refused {url}: too many redirects")
