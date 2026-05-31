"""Apply resource limits to an MCP subprocess.

Called from the subprocess's preexec_fn (so
the rlimits scope to the child, not the parent proxy process).
RLIMIT_AS caps virtual memory; RLIMIT_CPU caps CPU seconds.

An OOM-bomb tool hits the memory cap and gets SIGSEGV from the
kernel.  A CPU-bomb tool hits SIGXCPU.  Either way the proxy pod
stays up and the call returns a structured error to the agent
instead of crashing the multi-tenant proxy process.
"""

from __future__ import annotations

import resource

__all__ = ["apply_rlimits"]


def apply_rlimits(*, memory_limit_mb: int, cpu_seconds: int) -> None:
    """Set RLIMIT_AS + RLIMIT_CPU in the current process.

    Called from ``preexec_fn`` so the limits apply to the child
    process post-fork, pre-exec.  Non-positive caps are skipped
    (defensive against config typos that would otherwise lock the
    subprocess into immediate failure on launch).
    """
    if memory_limit_mb > 0:
        mem_bytes = memory_limit_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))
    if cpu_seconds > 0:
        resource.setrlimit(
            resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds),
        )
