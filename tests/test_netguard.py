"""Refusing to fetch a private, loopback or link-local address.

Every address here is a literal IP, so `check` resolves it without any real
DNS lookup or network access — the same reason `requests.Session.get` itself
is stood in for below, the seam `test_pictures.py` uses for `_download`.
"""

from __future__ import annotations

import pytest
from requests.models import PreparedRequest

from textcast import netguard


def test_a_loopback_address_is_refused():
    with pytest.raises(netguard.UnsafeURL):
        netguard.check("http://127.0.0.1/")


def test_a_private_address_is_refused():
    with pytest.raises(netguard.UnsafeURL):
        netguard.check("http://10.0.0.5/")


def test_a_link_local_address_is_refused():
    with pytest.raises(netguard.UnsafeURL):
        netguard.check("http://169.254.169.254/")


def test_a_shared_cgnat_address_is_refused():
    """100.64.0.0/10 is not `is_private`, but it is the range Tailscale hands
    its own nodes — reachable, on a typical self-hosted deployment, in a way
    "public" should not include."""
    with pytest.raises(netguard.UnsafeURL):
        netguard.check("http://100.64.0.1/")


def test_a_public_address_is_allowed():
    netguard.check("http://93.184.216.34/")


def test_a_non_http_scheme_is_refused():
    with pytest.raises(netguard.UnsafeURL):
        netguard.check("file:///etc/passwd")


def test_the_pinned_adapter_swaps_the_checked_hostname_for_its_address():
    adapter = netguard._PinnedAdapter("example.com", "93.184.216.34", https=True)
    request = PreparedRequest()
    request.prepare(method="GET", url="https://example.com/path")

    host_params, pool_kwargs = adapter.build_connection_pool_key_attributes(
        request, True
    )

    assert host_params["host"] == "93.184.216.34"
    # The certificate is still checked against the real hostname -- pinning
    # is only where the socket connects, not what TLS trusts.
    assert pool_kwargs["assert_hostname"] == "example.com"
    assert pool_kwargs["server_hostname"] == "example.com"


def test_the_pinned_adapter_leaves_a_plain_http_pool_key_untouched():
    """HTTPConnection (unlike HTTPSConnection) takes no `assert_hostname` or
    `server_hostname` kwarg -- passing either raises, so plain http must not
    get them."""
    adapter = netguard._PinnedAdapter("example.com", "93.184.216.34", https=False)
    request = PreparedRequest()
    request.prepare(method="GET", url="http://example.com/path")

    host_params, pool_kwargs = adapter.build_connection_pool_key_attributes(
        request, True
    )

    assert host_params["host"] == "93.184.216.34"
    assert "assert_hostname" not in pool_kwargs
    assert "server_hostname" not in pool_kwargs


class FakeResponse:
    def __init__(self, *, redirect_to: str | None = None):
        self.is_redirect = redirect_to is not None
        self.headers = {"Location": redirect_to} if redirect_to else {}
        self.closed = False

    def close(self):
        self.closed = True


def test_get_checks_a_redirects_target_before_following_it(monkeypatch):
    calls: list[str] = []

    def fake_get(self, url, **kwargs):
        calls.append(url)
        if url == "http://93.184.216.34/":
            return FakeResponse(redirect_to="http://169.254.169.254/secret")
        raise AssertionError("must not connect past the unsafe redirect")

    monkeypatch.setattr(netguard.requests.Session, "get", fake_get)
    with pytest.raises(netguard.UnsafeURL):
        netguard.get("http://93.184.216.34/")
    assert calls == ["http://93.184.216.34/"]


def test_get_follows_a_safe_redirect_to_a_safe_target(monkeypatch):
    calls: list[str] = []

    def fake_get(self, url, **kwargs):
        calls.append(url)
        if url == "http://93.184.216.34/":
            return FakeResponse(redirect_to="http://93.184.216.34/final")
        return FakeResponse()

    monkeypatch.setattr(netguard.requests.Session, "get", fake_get)
    response = netguard.get("http://93.184.216.34/")
    assert not response.is_redirect
    assert calls == ["http://93.184.216.34/", "http://93.184.216.34/final"]


def test_a_redirect_loop_gives_up_rather_than_following_it_forever(monkeypatch):
    monkeypatch.setattr(
        netguard.requests.Session,
        "get",
        lambda self, url, **kw: FakeResponse(redirect_to=url),
    )
    with pytest.raises(netguard.UnsafeURL):
        netguard.get("http://93.184.216.34/")
