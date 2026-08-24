"""Sandbox isolation and process-wide egress enforcement.

Both replace guidance with enforcement. docs/SECURITY.md argued for "root
inside a disposable sandbox" and for an egress allowlist; neither had code
behind it, and an allowlist only consulted by callers who remember it is
documentation rather than a control.
"""

from __future__ import annotations

import os
import socket
import threading

import pytest

from cip.security import netguard
from cip.security.netguard import EgressBlocked, enforce_egress
from cip.security.sandbox import (SandboxLimits, enforceable_limits,
                                  run_sandboxed)


# --- sandbox ---------------------------------------------------------------
def test_parent_credentials_do_not_reach_the_child(monkeypatch):
    """An allowlist, not a denylist: a new credential variable is dropped by
    default rather than after someone remembers to add it."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-not-leak")
    monkeypatch.setenv("DATABRICKS_TOKEN", "dapi-should-not-leak")

    result = run_sandboxed(
        ["python3", "-c",
         "import os;print([k for k in os.environ if 'KEY' in k or 'TOKEN' in k])"])
    assert result.ok
    assert "ANTHROPIC_API_KEY" not in result.stdout
    assert "DATABRICKS_TOKEN" not in result.stdout


def test_child_runs_in_a_disposable_jail():
    result = run_sandboxed(["python3", "-c", "import os;print(os.getcwd())"])
    jail = result.stdout.strip()
    assert "cip-sandbox-" in jail
    assert not os.path.exists(jail), "the jail must not outlive the task"


def test_runaway_process_is_killed_on_wall_clock():
    """CPU limits do not catch a process that sleeps, and a slot held is a
    slot denied to real work."""
    result = run_sandboxed(["python3", "-c", "import time;time.sleep(30)"],
                           limits=SandboxLimits(wall_clock_seconds=2))
    assert not result.ok
    assert "wall clock" in result.reason


def test_platform_limits_are_reported_not_assumed():
    """macOS ignores RLIMIT_AS. A sandbox that claims a limit it does not
    apply is worse than one that says which limits it has."""
    limits = enforceable_limits()
    assert limits["RLIMIT_CPU"] is True
    assert set(limits) >= {"RLIMIT_CPU", "RLIMIT_FSIZE", "RLIMIT_CORE"}
    assert all(isinstance(v, bool) for v in limits.values())


def test_a_failed_command_is_reported_not_raised():
    result = run_sandboxed(["python3", "-c", "raise SystemExit(3)"])
    assert not result.ok and result.returncode == 3


# --- egress ----------------------------------------------------------------
@pytest.fixture()
def listener():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    threading.Thread(target=lambda: server.accept(), daemon=True).start()
    yield server.getsockname()[1]
    server.close()


def test_loopback_is_not_egress(listener):
    """Test servers and local databases are inside the boundary."""
    with enforce_egress(allowlist=("example.com",)):
        socket.create_connection(("127.0.0.1", listener), timeout=2).close()


def test_unlisted_destination_is_blocked_at_the_socket(listener, monkeypatch):
    """Deterministic: the address is pre-mapped to a hostname so the test
    needs no DNS and no internet."""
    monkeypatch.setitem(netguard._resolved, "127.0.0.1", "attacker-drop.xyz")
    with enforce_egress(allowlist=("s3.amazonaws.com",)):
        with pytest.raises(EgressBlocked):
            socket.create_connection(("127.0.0.1", listener), timeout=2)


def test_allowlisted_destination_passes(listener, monkeypatch):
    monkeypatch.setitem(netguard._resolved, "127.0.0.1", "s3.amazonaws.com")
    with enforce_egress(allowlist=("s3.amazonaws.com",)):
        socket.create_connection(("127.0.0.1", listener), timeout=2).close()


def test_the_guard_is_removed_afterwards(listener):
    """Patching sockets for the whole interpreter would be hostile to
    anything else running in it."""
    original = socket.socket.connect
    with enforce_egress(allowlist=("example.com",)):
        assert socket.socket.connect is not original
    assert socket.socket.connect is original
    assert socket.getaddrinfo is netguard._original_getaddrinfo


def test_dns_rebinding_cannot_launder_a_blocked_host(listener, monkeypatch):
    """The bypass this ordering exists to close.

    An attacker-controlled domain that resolves to 127.0.0.1 would pass a
    guard that exempts loopback before looking at the hostname -- the
    address is local, so the check never runs. The hostname is resolved
    first, so the exemption applies only to literal loopback connections
    with no name behind them.
    """
    monkeypatch.setitem(netguard._resolved, "127.0.0.1", "evil.example.com")
    with enforce_egress(allowlist=("s3.amazonaws.com",)):
        with pytest.raises(EgressBlocked):
            socket.create_connection(("127.0.0.1", listener), timeout=2)
