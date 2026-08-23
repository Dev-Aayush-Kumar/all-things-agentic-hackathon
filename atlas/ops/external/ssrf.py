"""Destination policy for FETCH_URL. Fail closed. Not a perfect SSRF shield."""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlparse

from atlas.config.settings import Settings
from atlas.domain.exceptions import ExternalToolValidationError

BLOCKED_HOSTNAMES = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "ip6-localhost",
        "ip6-loopback",
        "metadata",
        "metadata.google.internal",
        "metadata.google.com",
    }
)
LINK_LOCAL_DNS = frozenset({"localhost", "metadata.google.internal"})
CGNAT = ipaddress.ip_network("100.64.0.0/10")
THIS_NETWORK = ipaddress.ip_network("0.0.0.0/8")
BENCHMARK = ipaddress.ip_network("198.18.0.0/15")
IPV4_BROADCAST = ipaddress.ip_network("255.255.255.255/32")


@dataclass(frozen=True)
class ValidatedDestination:
    """A URL that passed scheme, host, and network checks."""

    original_url: str
    normalized_url: str
    hostname: str
    scheme: str
    resolved_ips: tuple[str, ...]


def validate_destination(url: str, settings: Settings) -> ValidatedDestination:
    """Reject private/loopback/link-local destinations and disallowed hosts."""
    if not isinstance(url, str) or not url.strip():
        raise ExternalToolValidationError("FETCH_URL requires a non-empty url")
    candidate = url.strip()
    if "\x00" in candidate or "\r" in candidate or "\n" in candidate:
        raise ExternalToolValidationError("URL contains invalid control characters")
    parsed = urlparse(candidate)
    scheme = (parsed.scheme or "").lower()
    if scheme not in settings.fetch_allowed_scheme_list:
        raise ExternalToolValidationError(f"Scheme '{parsed.scheme}' is not allowed")
    if parsed.username or parsed.password:
        raise ExternalToolValidationError("URLs may not include credentials")
    if parsed.port == 0:
        raise ExternalToolValidationError("URL port is invalid")
    hostname = (parsed.hostname or "").strip().lower().rstrip(".")
    if not hostname:
        raise ExternalToolValidationError("URL is missing a hostname")
    if _hostname_blocked(hostname) and not _loopback_explicitly_allowed(hostname, settings):
        raise ExternalToolValidationError(f"Host '{hostname}' is not allowed")

    resolved: list[str] = []
    ip_literal = _as_ip(hostname)
    if ip_literal is not None:
        _assert_ip_allowed(ip_literal, settings, hostname)
        resolved.append(str(ip_literal))
    else:
        resolved = _resolve_and_check(hostname, settings)

    if not _host_in_allowlist(hostname, settings.fetch_allowed_domain_list):
        raise ExternalToolValidationError(
            f"Host '{hostname}' is not on the FETCH_URL domain allowlist"
        )

    normalized = parsed._replace(
        scheme=scheme,
        netloc=parsed.netloc.lower(),
        fragment="",
    ).geturl()
    return ValidatedDestination(
        original_url=candidate,
        normalized_url=normalized,
        hostname=hostname,
        scheme=scheme,
        resolved_ips=tuple(resolved),
    )


def _hostname_blocked(hostname: str) -> bool:
    if hostname in BLOCKED_HOSTNAMES or hostname in LINK_LOCAL_DNS:
        return True
    if hostname.endswith(".localhost") or hostname.endswith(".local"):
        return True
    return False


def _loopback_explicitly_allowed(hostname: str, settings: Settings) -> bool:
    if not settings.fetch_allow_loopback:
        return False
    return _host_in_allowlist(hostname, settings.fetch_allowed_domain_list)


def _host_in_allowlist(hostname: str, allowed: list[str]) -> bool:
    if not allowed:
        return False
    host = hostname.lower().rstrip(".")
    for pattern in allowed:
        if not pattern:
            continue
        if pattern.startswith("*."):
            suffix = pattern[1:]
            if host.endswith(suffix) or host == pattern[2:]:
                return True
            continue
        if host == pattern or host.endswith("." + pattern):
            return True
    return False


def _as_ip(hostname: str):
    try:
        return ipaddress.ip_address(hostname)
    except ValueError:
        return None


def _resolve_and_check(hostname: str, settings: Settings) -> list[str]:
    try:
        answers = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise ExternalToolValidationError(
            f"Host '{hostname}' could not be resolved"
        ) from exc
    ips: list[str] = []
    for item in answers:
        raw = item[4][0]
        ip = _as_ip(raw)
        if ip is None:
            continue
        _assert_ip_allowed(ip, settings, hostname)
        text = str(ip)
        if text not in ips:
            ips.append(text)
    if not ips:
        raise ExternalToolValidationError(f"Host '{hostname}' resolved to no usable addresses")
    return ips


def _assert_ip_allowed(ip, settings: Settings, hostname: str) -> None:
    blocked = _ip_is_blocked(ip)
    if not blocked:
        return
    if settings.fetch_allow_loopback and _is_loopback_or_link_local_test(ip):
        if _host_in_allowlist(hostname, settings.fetch_allowed_domain_list):
            return
    raise ExternalToolValidationError(
        f"Destination address '{ip}' is not allowed"
    )


def _is_loopback_or_link_local_test(ip) -> bool:
    return bool(ip.is_loopback)


def _ip_is_blocked(ip) -> bool:
    if ip.version == 6 and ip.ipv4_mapped is not None:
        return _ip_is_blocked(ip.ipv4_mapped)
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        return True
    if ip.version == 4:
        packed = ipaddress.ip_network(ip)
        if (
            packed.overlaps(CGNAT)
            or packed.overlaps(THIS_NETWORK)
            or packed.overlaps(BENCHMARK)
            or packed.overlaps(IPV4_BROADCAST)
        ):
            return True
    return False
