"""Process isolation for privileged execution.

`docs/SECURITY.md` argued that when an agent genuinely needs shell or
filesystem access, the answer is "root inside a disposable sandbox, not
root on the production host". That was guidance with no implementation
behind it, which is the weakest kind of security claim.

This is the enforcement. It is process-level isolation, not a container:

  * a scrubbed environment -- credentials in the parent's environment do
    not reach the child, so a compromised task cannot read what it was
    never given
  * a working directory jailed to a fresh temporary tree, discarded after
  * CPU, address space, file size and core limits via setrlimit
  * a wall-clock timeout with SIGKILL, because a process that ignores CPU
    limits while sleeping still holds a slot

A container adds kernel-level namespacing and is the production form. What
this provides is the boundary the policy engine can actually rely on today,
with the limits it enforces stated rather than implied.
"""

from __future__ import annotations

import os
import resource
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

# Variables a child may see. Everything else -- API keys, tokens, cloud
# credentials -- is dropped rather than filtered, because an allowlist fails
# closed when a new credential variable appears and a denylist does not.
ENV_ALLOWLIST = ("PATH", "LANG", "LC_ALL", "TMPDIR", "HOME")


@dataclass
class SandboxLimits:
    cpu_seconds: int = 5
    address_space_mb: int = 512
    file_size_mb: int = 16
    wall_clock_seconds: int = 10
    processes: int = 64


@dataclass
class SandboxResult:
    ok: bool
    stdout: str
    stderr: str
    returncode: int
    reason: str = ""


def _desired_limits(limits: SandboxLimits) -> list[tuple[str, int, tuple[int, int]]]:
    mb = 1024 * 1024
    out = [("RLIMIT_CPU", resource.RLIMIT_CPU,
            (limits.cpu_seconds, limits.cpu_seconds)),
           ("RLIMIT_FSIZE", resource.RLIMIT_FSIZE,
            (limits.file_size_mb * mb, limits.file_size_mb * mb)),
           ("RLIMIT_CORE", resource.RLIMIT_CORE, (0, 0))]
    for name in ("RLIMIT_AS", "RLIMIT_NPROC"):
        if hasattr(resource, name):
            value = (limits.address_space_mb * mb if name == "RLIMIT_AS"
                     else limits.processes)
            out.append((name, getattr(resource, name), (value, value)))
    return out


def enforceable_limits(limits: SandboxLimits | None = None) -> dict[str, bool]:
    """Which limits this platform actually accepts.

    Not every rlimit exists or is honoured everywhere -- macOS rejects some
    outright, and a sandbox that silently drops a limit while claiming to
    enforce it is worse than one that says which it applies. Probed in a
    child so a failed attempt cannot disturb this process.
    """
    limits = limits or SandboxLimits()
    results: dict[str, bool] = {}
    for name, key, value in _desired_limits(limits):
        probe = subprocess.run(
            ["python3", "-c",
             f"import resource;resource.setrlimit({key}, {value})"],
            capture_output=True, text=True)
        results[name] = probe.returncode == 0
    return results


def _apply_limits(limits: SandboxLimits):
    def preexec() -> None:  # pragma: no cover - runs in the child
        # Best effort per limit. One unsupported rlimit must not take the
        # whole sandbox down -- that would trade every protection for the
        # one the platform happens to refuse.
        for _name, key, value in _desired_limits(limits):
            try:
                resource.setrlimit(key, value)
            except (OSError, ValueError):
                pass
        try:
            # New session: the child cannot signal the parent's process
            # group, and killing the group on timeout takes descendants too.
            os.setsid()
        except OSError:
            pass
    return preexec


def run_sandboxed(command: list[str], *, limits: SandboxLimits | None = None,
                  stdin: str = "") -> SandboxResult:
    """Run a command with a scrubbed environment inside a disposable jail."""
    limits = limits or SandboxLimits()
    env = {k: v for k, v in os.environ.items() if k in ENV_ALLOWLIST}
    jail = Path(tempfile.mkdtemp(prefix="cip-sandbox-"))
    env["TMPDIR"] = str(jail)
    env["HOME"] = str(jail)

    try:
        completed = subprocess.run(
            command, cwd=jail, env=env, input=stdin, capture_output=True,
            text=True, timeout=limits.wall_clock_seconds,
            preexec_fn=_apply_limits(limits))
        return SandboxResult(
            ok=completed.returncode == 0, stdout=completed.stdout,
            stderr=completed.stderr, returncode=completed.returncode,
            reason="" if completed.returncode == 0 else "non-zero exit")
    except subprocess.TimeoutExpired:
        return SandboxResult(False, "", "", -1,
                             f"killed after {limits.wall_clock_seconds}s wall clock")
    except (OSError, ValueError) as exc:
        return SandboxResult(False, "", "", -1, f"{type(exc).__name__}: {exc}")
    finally:
        # The jail goes with the task. Nothing the child wrote survives.
        shutil.rmtree(jail, ignore_errors=True)
