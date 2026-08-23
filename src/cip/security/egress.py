"""Egress control.

The last line of defence. Even if injection detection misses, taint
tracking is bypassed and the model is fully convinced it should exfiltrate
data, the packet still has to leave through here -- and only allowlisted
destinations exist.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from urllib.parse import urlparse

from ..config import CONFIG
from .dlp import scan


@dataclass(frozen=True)
class EgressDecision:
    allowed: bool
    destination: str
    reason: str


def _host_of(url: str) -> str:
    parsed = urlparse(url if "://" in url else f"https://{url}")
    return (parsed.hostname or "").lower()


def check_destination(url: str, payload: str = "", allowlist: tuple[str, ...] | None = None) -> EgressDecision:
    allowed_hosts = allowlist if allowlist is not None else CONFIG.egress_allowlist
    host = _host_of(url)

    if not host:
        return EgressDecision(False, url, "unparseable destination")

    # Block SSRF into link-local metadata regardless of allowlist contents.
    try:
        addr = ipaddress.ip_address(host)
        if addr.is_link_local or addr.is_loopback or addr.is_private:
            return EgressDecision(False, host, f"private/link-local address {host} (SSRF guard)")
    except ValueError:
        pass

    if not any(host == a or host.endswith("." + a) for a in allowed_hosts):
        return EgressDecision(False, host, f"host not in egress allowlist {allowed_hosts}")

    secrets = [f.kind for f in scan(payload) if f.is_secret]
    if secrets:
        return EgressDecision(False, host, f"payload contains secrets: {sorted(set(secrets))}")

    return EgressDecision(True, host, "allowlisted destination, payload clean")
