"""Process-wide egress enforcement.

`egress.check_destination()` decides whether a destination is allowed, but
deciding is not enforcing: any code that called `requests`, `httpx` or a
raw socket bypassed it entirely. An allowlist that only applies to callers
who remember to consult it is documentation.

This installs the check underneath the socket layer, so every outbound TCP
connection in the process passes it regardless of which library opened it.
That is the property the security model claims -- "even if injection
convinces the model to exfiltrate, the packet still has to leave through
here".

DNS resolution is intentionally left alone: name lookups leak far less than
a connection, and blocking them produces failures that look like network
outages rather than policy denials.
"""

from __future__ import annotations

import socket
import threading
from contextlib import contextmanager

from .audit import audit
from .egress import check_destination

_original_connect = socket.socket.connect
_original_getaddrinfo = socket.getaddrinfo

# ip -> hostname, populated by the patched resolver.
#
# `connect()` is handed an address that DNS has already resolved, so the
# hostname is gone by the time the guard sees it. Comparing an IP against a
# hostname allowlist denies everything -- which looks like a working guard,
# because the attacks are blocked too, while every legitimate destination is
# blocked as well. The resolver is where the name still exists.
_resolved: dict[str, str] = {}
_resolved_lock = threading.Lock()


class EgressBlocked(OSError):
    """Raised in place of completing a denied connection."""


def _is_ip_literal(value: str) -> bool:
    import ipaddress

    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def _guarded_getaddrinfo(*args, **kwargs):
    """Record which hostname produced which addresses."""
    results = _original_getaddrinfo(*args, **kwargs)
    hostname = args[0] if args else kwargs.get("host")
    # Skip IP literals. Recording "127.0.0.1" as its own hostname makes the
    # address look like it came from a lookup, which defeats the loopback
    # exemption and blocks every local connection.
    if isinstance(hostname, str) and not _is_ip_literal(hostname):
        with _resolved_lock:
            for entry in results:
                address = entry[4][0]
                if isinstance(address, str):
                    _resolved[address] = hostname
    return results


def _guarded_connect(allowlist: tuple[str, ...]):
    def connect(self, address):  # pragma: no cover - exercised via the fixture
        host = address[0] if isinstance(address, tuple) else None
        if host is None:                      # unix sockets and the like
            return _original_connect(self, address)

        # Resolve back to the name the caller asked for BEFORE any local
        # exemption. Exempting loopback first is a DNS-rebinding bypass: an
        # attacker-controlled domain that resolves to 127.0.0.1 would sail
        # through a check that only ever saw the address.
        with _resolved_lock:
            resolved_from = _resolved.get(host)

        if resolved_from is not None:
            host = resolved_from
        elif host in ("127.0.0.1", "::1", "localhost"):
            # A literal loopback connection with no hostname behind it:
            # test servers and local databases are inside the boundary.
            return _original_connect(self, address)
        # Anything else with no recorded lookup is a direct-to-IP
        # connection, checked as-is. Skipping DNS is a way to dodge a
        # hostname allowlist, not a reason to be trusted.

        decision = check_destination(host, allowlist=allowlist)
        if not decision.allowed:
            audit.write("egress_blocked_at_socket", host=str(host),
                        reason=decision.reason)
            raise EgressBlocked(
                f"egress to {host} blocked by policy: {decision.reason}")
        return _original_connect(self, address)
    return connect


@contextmanager
def enforce_egress(allowlist: tuple[str, ...] | None = None):
    """Install the guard for the duration of the block.

    Scoped rather than global on import: a library that patches sockets for
    the whole interpreter is hostile to anything else running in it, and
    the guard belongs around the untrusted work, not around everything.
    """
    from ..config import CONFIG

    allowed = allowlist if allowlist is not None else CONFIG.egress_allowlist
    socket.getaddrinfo = _guarded_getaddrinfo
    socket.socket.connect = _guarded_connect(allowed)
    try:
        yield
    finally:
        socket.socket.connect = _original_connect
        socket.getaddrinfo = _original_getaddrinfo
