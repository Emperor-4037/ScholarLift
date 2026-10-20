"""Tests for URL canonicalization and SSRF protection."""
from __future__ import annotations

import pytest

from scholarship_finder.domain.url import (
    canonicalize_url,
    extract_domain,
    is_private_or_loopback,
    is_same_domain,
)


class TestCanonicalizeUrl:
    def test_strips_utm_params(self):
        url = "https://mit.edu/admissions?utm_source=google&utm_medium=cpc"
        result = canonicalize_url(url)
        assert "utm_source" not in result
        assert "utm_medium" not in result
        assert "mit.edu" in result

    def test_strips_fbclid(self):
        url = "https://example.edu/page?fbclid=abc123&real=keep"
        result = canonicalize_url(url)
        assert "fbclid" not in result
        assert "real=keep" in result

    def test_removes_fragment(self):
        url = "https://mit.edu/page#section"
        assert "#" not in canonicalize_url(url)

    def test_removes_www_prefix(self):
        result = canonicalize_url("https://www.mit.edu/")
        assert "www." not in result

    def test_normalises_http_to_https(self):
        result = canonicalize_url("http://mit.edu/")
        assert result.startswith("https://")

    def test_removes_trailing_slash(self):
        a = canonicalize_url("https://mit.edu/admissions/")
        b = canonicalize_url("https://mit.edu/admissions")
        assert a == b

    def test_sorts_query_params(self):
        a = canonicalize_url("https://example.com/?z=1&a=2")
        b = canonicalize_url("https://example.com/?a=2&z=1")
        assert a == b

    def test_lowercases_host(self):
        result = canonicalize_url("https://MIT.EDU/")
        assert "MIT" not in result
        assert "mit.edu" in result

    def test_dedup_same_url(self):
        u1 = "https://www.daad.de/en/study/?utm_source=test"
        u2 = "https://daad.de/en/study/"
        assert canonicalize_url(u1) == canonicalize_url(u2)

    def test_bare_root_path_preserved(self):
        result = canonicalize_url("https://mit.edu/")
        assert result.endswith("/") or result == "https://mit.edu"


class TestExtractDomain:
    def test_basic(self):
        assert extract_domain("https://www.mit.edu/page") == "mit.edu"

    def test_strips_port(self):
        assert extract_domain("https://example.com:8080/path") == "example.com"

    def test_subdomain_kept(self):
        assert extract_domain("https://admissions.example.edu/") == "admissions.example.edu"


class TestIsSameDomain:
    def test_same_domain(self):
        assert is_same_domain("https://www.mit.edu/a", "https://mit.edu/b")

    def test_different_domain(self):
        assert not is_same_domain("https://mit.edu/", "https://stanford.edu/")


class TestPrivateOrLoopback:
    def test_localhost(self):
        assert is_private_or_loopback("http://localhost/")

    def test_loopback_ip(self):
        assert is_private_or_loopback("http://127.0.0.1/secret")

    def test_private_class_a(self):
        assert is_private_or_loopback("http://10.0.0.1/internal")

    def test_private_class_c(self):
        assert is_private_or_loopback("http://192.168.1.100/")

    def test_link_local(self):
        assert is_private_or_loopback("http://169.254.0.1/")

    def test_public_ip_ok(self):
        assert not is_private_or_loopback("https://8.8.8.8/")

    def test_public_domain_ok(self):
        assert not is_private_or_loopback("https://mit.edu/")

    def test_blank_host_rejected(self):
        assert is_private_or_loopback("https:///path")
