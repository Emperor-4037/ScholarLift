"""Deterministic URL canonicalization and SSRF-protection utilities.

Uses only `urllib.parse` — no external dependencies.
"""
from __future__ import annotations

import ipaddress
from urllib.parse import urlparse, urlunparse, urlencode, parse_qsl

# Query-string parameters that carry no information value and should be stripped
_TRACKING_PARAMS: frozenset[str] = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_id", "utm_reader", "utm_name", "utm_place",
    "fbclid", "gclid", "msclkid", "dclid", "yclid",
    "ref", "referrer", "source", "mc_cid", "mc_eid",
    "_ga", "_gl", "igshid", "s_cid", "ncid",
})

# Loopback / private / link-local ranges (IPv4 + IPv6)
_PRIVATE_NETWORKS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (
    ipaddress.IPv4Network("127.0.0.0/8"),
    ipaddress.IPv4Network("10.0.0.0/8"),
    ipaddress.IPv4Network("172.16.0.0/12"),
    ipaddress.IPv4Network("192.168.0.0/16"),
    ipaddress.IPv4Network("169.254.0.0/16"),    # link-local
    ipaddress.IPv4Network("0.0.0.0/8"),
    ipaddress.IPv6Network("::1/128"),            # loopback
    ipaddress.IPv6Network("fc00::/7"),           # unique-local
    ipaddress.IPv6Network("fe80::/10"),          # link-local
)


def canonicalize_url(url: str) -> str:
    """Return a canonical form of *url* suitable for deduplication.

    Steps applied:
    1. Normalise scheme to ``https``; accept ``http`` only.
    2. Lowercase the hostname.
    3. Remove ``www.`` prefix.
    4. Strip tracking query parameters.
    5. Sort remaining query parameters.
    6. Remove the URL fragment.
    7. Remove a trailing ``/`` from the path (but keep bare ``/``).
    """
    url = url.strip()
    parsed = urlparse(url)

    # 1. Enforce https scheme
    scheme = "https" if parsed.scheme in ("http", "https") else parsed.scheme

    # 2 & 3. Lowercase host, remove www.
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]

    # 4. Strip tracking params; 5. Sort
    qs = parse_qsl(parsed.query, keep_blank_values=False)
    qs_clean = sorted((k, v) for k, v in qs if k.lower() not in _TRACKING_PARAMS)
    query = urlencode(qs_clean)

    # 6. Remove fragment; 7. Strip trailing slash
    path = parsed.path
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")

    return urlunparse((scheme, host, path, parsed.params, query, ""))


def extract_domain(url: str) -> str:
    """Return the (www-stripped, lowercased) domain of *url*."""
    host = urlparse(url).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    # Strip port if present
    return host.split(":")[0]


def is_same_domain(url1: str, url2: str) -> bool:
    """True when both URLs share the same canonical domain."""
    return extract_domain(url1) == extract_domain(url2)


def is_private_or_loopback(url: str) -> bool:
    """SSRF protection: True when the URL resolves to a private/loopback address.

    Checks the hostname literally; does not perform DNS resolution (that
    happens in the Fetch adapter with the same guard).
    """
    host = extract_domain(url)
    if not host:
        return True   # blank host — reject

    # Reject obvious loopback hostnames
    if host in ("localhost", "localhost.localdomain", "0.0.0.0", "::1"):
        return True

    # Try numeric IP
    try:
        addr = ipaddress.ip_address(host)
        return any(addr in net for net in _PRIVATE_NETWORKS)
    except ValueError:
        pass  # Not an IP literal; allow (DNS guard is in the adapter)

    return False
