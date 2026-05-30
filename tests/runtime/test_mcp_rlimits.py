"""Tests for the apply_rlimits helper.

Plan 5 / Task 10.  RLIMIT_AS (virtual memory) and RLIMIT_CPU
(CPU seconds) get applied in the subprocess's preexec_fn so an
OOM-bomb or fork-bomb tool cannot take down the proxy pod.
"""

from __future__ import annotations

import resource


def test_apply_rlimits_sets_memory_cap_via_subprocess():
    """Drive apply_rlimits via preexec_fn in a subprocess and verify
    the cap took effect — running it in the parent is unsafe because
    setrlimit lowers the hard cap, which cannot be raised again
    (CAP_SYS_RESOURCE is unprivileged).  preexec_fn is the actual
    use site (MCPCallSandbox / Task 9) so the subprocess path is
    the meaningful regression."""
    import subprocess
    import sys

    from surogates.mcp_proxy.rlimits import apply_rlimits

    def _preexec():
        apply_rlimits(memory_limit_mb=64, cpu_seconds=10)

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import resource;"
                "s, h = resource.getrlimit(resource.RLIMIT_AS);"
                "c, _ = resource.getrlimit(resource.RLIMIT_CPU);"
                "print(s, c)"
            ),
        ],
        capture_output=True,
        text=True,
        preexec_fn=_preexec,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    mem_soft, cpu_soft = result.stdout.split()
    assert int(mem_soft) <= 64 * 1024 * 1024
    assert int(cpu_soft) <= 10


def test_apply_rlimits_ignores_negative_or_zero_caps():
    """Defensive: a misconfigured cap (e.g., 0 from a typo) should
    not lock the subprocess into immediate failure.  The helper
    skips the rlimit call when the cap is non-positive."""
    from surogates.mcp_proxy.rlimits import apply_rlimits

    soft_before, _ = resource.getrlimit(resource.RLIMIT_AS)
    apply_rlimits(memory_limit_mb=0, cpu_seconds=-1)
    soft_after, _ = resource.getrlimit(resource.RLIMIT_AS)
    assert soft_after == soft_before
