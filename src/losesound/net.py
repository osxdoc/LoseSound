"""Network helpers.

Some home networks have broken IPv6 connectivity. Python's ``urllib`` connects to
the first address returned by the resolver and does not retry another family when
the TCP connection succeeds but the TLS handshake is reset. That affects the
radio-browser API in particular. Prefer IPv4 for outbound requests by default;
set ``NET_FORCE_IPV4=0`` to keep the system default.
"""

from __future__ import annotations

import os
import socket

_INSTALLED = False


def prefer_ipv4() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    value = os.getenv("NET_FORCE_IPV4", "1").strip().lower()
    if value not in ("1", "true", "yes", "on"):
        _INSTALLED = True
        return

    original = socket.getaddrinfo

    def getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):  # noqa: A002 - mirrors socket API
        if family in (0, socket.AF_UNSPEC):
            try:
                return original(host, port, socket.AF_INET, type, proto, flags)
            except socket.gaierror:
                return original(host, port, family, type, proto, flags)
        return original(host, port, family, type, proto, flags)

    getaddrinfo._losesound_ipv4 = True  # type: ignore[attr-defined]
    socket.getaddrinfo = getaddrinfo
    _INSTALLED = True
