"""Grade a task by running upstream's evaluator in its task image.

Each task's ``evaluator.py`` expects to run *inside* the task's own
container: the shared ``scoring``/``common`` modules on the path, the
agent's files at ``/workspace``, and the company services reachable
under their canonical hostname. This module builds exactly that one
``docker run``: the task image (built by upstream's
``evaluation/generate_task_images.py``), the collected workspace
mounted read-only at ``/workspace``, an ``--add-host`` mapping the
canonical hostname to the operator's stack, and a python one-liner that
calls ``grade_checkpoints()`` and prints the Result as JSON.

The container is upstream's, byte for byte -- this file only launches
it and parses the verdict. A task whose image is missing or whose
evaluator crashes is **ungradable with the reason**, never zero.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field

GRADER_TIMEOUT_S = 900

# Runs inside the task image. /utils is where upstream's base image
# keeps evaluator.py, scoring.py and common.py.
_INLINE = (
    "import json, sys; sys.path.insert(0, '/utils'); "
    "import evaluator; "
    "r = evaluator.grade_checkpoints(); "
    "print('TACRESULT ' + json.dumps({'checkpoints': "
    "[{'total': c.total, 'result': c.result} for c in r.checkpoints], "
    "'final': r.final_score}))"
)


@dataclass
class GradeResult:
    task_id: str
    points_total: int | None
    points_earned: int | None
    checkpoints: list[dict] = field(default_factory=list)
    grade_error: str | None = None


def image_name(task_id: str, registry: str) -> str:
    return f"{registry.rstrip('/')}/{task_id}-image:latest"


def build_command(
    task_id: str,
    workspace_dir: str,
    hostname_ip: str,
    registry: str,
) -> list[str]:
    return [
        "docker", "run", "--rm",
        "--add-host", f"the-agent-company.com:{hostname_ip}",
        "-v", f"{workspace_dir}:/workspace:ro",
        image_name(task_id, registry),
        "python", "-c", _INLINE,
    ]


def parse_result(stdout: str, task_id: str) -> GradeResult:
    for line in reversed(stdout.strip().splitlines()):
        if line.startswith("TACRESULT "):
            data = json.loads(line.removeprefix("TACRESULT "))
            final = data.get("final") or {}
            return GradeResult(
                task_id=task_id,
                points_total=int(final.get("total", 0)),
                points_earned=int(final.get("result", 0)),
                checkpoints=list(data.get("checkpoints") or []),
            )
    return GradeResult(
        task_id=task_id, points_total=None, points_earned=None,
        grade_error=f"evaluator printed no TACRESULT line: {stdout[-300:]!r}",
    )


def grade_task(
    task_id: str,
    workspace_dir: str,
    hostname_ip: str,
    registry: str,
    runner=subprocess.run,
) -> GradeResult:
    command = build_command(task_id, workspace_dir, hostname_ip, registry)
    try:
        proc = runner(
            command, capture_output=True, text=True, timeout=GRADER_TIMEOUT_S
        )
    except subprocess.TimeoutExpired:
        return GradeResult(task_id=task_id, points_total=None,
                           points_earned=None,
                           grade_error=f"evaluator timed out after {GRADER_TIMEOUT_S}s")
    except FileNotFoundError:
        return GradeResult(task_id=task_id, points_total=None,
                           points_earned=None,
                           grade_error="docker not found on PATH")
    if proc.returncode != 0:
        return GradeResult(
            task_id=task_id, points_total=None, points_earned=None,
            grade_error=(proc.stderr.strip() or proc.stdout.strip()
                         or f"evaluator exited {proc.returncode}")[-400:],
        )
    return parse_result(proc.stdout, task_id)
