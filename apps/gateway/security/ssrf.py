"""SSRF validation for user-supplied provider and callback URLs."""
from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import urlsplit

_ALLOWED_SCHEMES = {"http", "https"}
_BLOCKED_HOSTNAMES = {
    "localhost",
    "localhost.localdomain",
    "metadata",
    "metadata.google.internal",
}
_CLOUD_METADATA_IPS = {
    ipaddress.ip_address("169.254.169.254"),
    ipaddress.ip_address("fd00:ec2::254"),
}


class ProviderURLValidationError(ValueError):
    """Raised when a provider URL is unsupported or resolves to a blocked target."""


def validate_provider_url(url: str, *, allow_private_network: bool = False) -> str:
    """Validate a provider URL and reject default SSRF targets unless explicitly allowed."""
    candidate = str(url or "").strip()
    parts = urlsplit(candidate)
    try:
        port = parts.port
    except ValueError as exc:
        raise ProviderURLValidationError("provider URL has an invalid port") from exc
    if parts.scheme.lower() not in _ALLOWED_SCHEMES:
        raise ProviderURLValidationError("provider URL must use http or https")
    if not parts.netloc or not parts.hostname:
        raise ProviderURLValidationError("provider URL must include a hostname")
    if parts.username or parts.password:
        raise ProviderURLValidationError("provider URL credentials are not allowed")
    host = parts.hostname.strip("[]")
    if host.lower() in _BLOCKED_HOSTNAMES:
        raise ProviderURLValidationError("provider URL hostname is not allowed")
    if parts.fragment or parts.query:
        raise ProviderURLValidationError("provider URL must not include query or fragment")

    try:
        literal = ipaddress.ip_address(host)
        addresses = {literal}
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, port or (443 if parts.scheme.lower() == "https" else 80), proto=socket.IPPROTO_TCP)
        except (UnicodeError, OSError):
            # Unresolvable hostnames cannot reach an internal address from here;
            # the outbound request itself will fail. Only resolved addresses are policy targets.
            infos = []
        addresses = {ipaddress.ip_address(info[4][0]) for info in infos}

    for address in addresses:
        blocked = (
            address in _CLOUD_METADATA_IPS
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
        )
        if not allow_private_network:
            blocked = blocked or address.is_private or address.is_loopback
        if blocked:
            raise ProviderURLValidationError("provider URL resolves to a disallowed network address")
    return candidate


def revalidate_provider_redirect(source: str, target: str, *, allow_private_network: bool = False) -> str:
    """Reject protocol downgrade and host escape before httpx follows a redirect."""
    source_parts = urlsplit(source)
    target_parts = urlsplit(target)
    target_url = validate_provider_url(target, allow_private_network=allow_private_network)
    if source_parts.scheme.lower() == "https" and target_parts.scheme.lower() != "https":
        raise ProviderURLValidationError("HTTPS provider redirects may not downgrade to HTTP")
    if source_parts.hostname and target_parts.hostname and source_parts.hostname.lower() != target_parts.hostname.lower():
        raise ProviderURLValidationError("provider redirects may not change the host")
    return target_url
