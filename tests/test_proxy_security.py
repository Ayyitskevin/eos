"""Spoof-resistant client identity at the reverse-proxy boundary."""

from eos import security
from starlette.requests import Request


def _request(peer: str | None, *headers: tuple[str, str]) -> Request:
    encoded_headers = [(name.lower().encode(), value.encode()) for name, value in headers]
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "https",
            "path": "/",
            "raw_path": b"/",
            "query_string": b"",
            "headers": encoded_headers,
            "client": (peer, 12345) if peer is not None else None,
            "server": ("eos.example.test", 443),
        }
    )


def test_untrusted_peer_cannot_spoof_dedicated_or_legacy_proxy_headers() -> None:
    request = _request(
        "203.0.113.9",
        ("X-Eos-Client-IP", "198.51.100.7"),
        ("CF-Connecting-IP", "198.51.100.8"),
        ("X-Forwarded-For", "198.51.100.9"),
        ("X-Real-IP", "198.51.100.10"),
    )

    assert security.client_ip(request) == "203.0.113.9"


def test_loopback_proxy_may_supply_one_valid_client_ip() -> None:
    request = _request("127.0.0.1", ("X-Eos-Client-IP", "198.51.100.7"))

    assert security.client_ip(request) == "198.51.100.7"


def test_ipv6_loopback_proxy_and_ipv4_mapped_addresses_are_normalized() -> None:
    request = _request("::1", ("X-Eos-Client-IP", "::ffff:198.51.100.7"))

    assert security.client_ip(request) == "198.51.100.7"


def test_invalid_forwarded_value_falls_back_to_proxy_peer() -> None:
    request = _request("127.0.0.1", ("X-Eos-Client-IP", "198.51.100.7, 10.0.0.1"))

    assert security.client_ip(request) == "127.0.0.1"


def test_direct_client_identity_and_missing_peer_remain_useful() -> None:
    assert security.client_ip(_request("2001:db8::9")) == "2001:db8::9"
    assert security.client_ip(_request("testclient")) == "testclient"
    assert security.client_ip(_request(None)) == "?"
