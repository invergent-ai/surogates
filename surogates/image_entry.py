"""The tool executor's start in a session's own image (``python3 -I -m surogates.image_entry``).

Outside ``surogates.sandbox``, whose package imports read the environment: before anything of the executor reads it,

- the names the backend lists in ``SUROGATES_IMAGE_ENV_UNSET`` leave it: the
  image declared them, and the executor or its tools read them for their own
  control (``PROJECT_DIR`` would make it a thread's pod, ``TERMINAL_TIMEOUT``
  must be a number, ...).  The commands still get the image's values
  (``SUROGATES_IMAGE_ENV_SHADOWED``, ``surogates.tools.workspace_io.local``);
- the executor's own state (its process registry, workspace checkpoints) goes
  to a private folder, not under the image user's HOME, which may be inside the
  task's repository or not writable.
"""

from __future__ import annotations

import json
import os

#: The executor's own state in a session's own image.
STATE_DIR = "/tmp/.surogates"
#: Names the executor and the harness's tools read for their own control: an image's value of one never reaches them.
CONTROL_PREFIXES = ("SUROGATES_", "TOOL_EXECUTOR_", "TERMINAL_", "HISTORY_", "MCP_PROXY_", "SANDBOX_", "S3_",
                    "GEESEFS_")
CONTROL_NAMES = frozenset({"PROJECT_DIR", "WORKSPACE_DIR", "USER_ID", "ORG_ID", "FORWARDED_ALLOW_IPS",
                           "WEB_CONCURRENCY"})


def is_control(name: str) -> bool:
    return name in CONTROL_NAMES or name.startswith(CONTROL_PREFIXES)


def prepare(environ: dict | os._Environ = os.environ) -> None:
    for name in environ.get("SUROGATES_IMAGE_ENV_UNSET", "").split(","):
        if name:
            environ.pop(name, None)
    # Kubernetes: the worker never sees the image, so its environment is the init container's file; a control name
    # it declares leaves unless the pod set it (``SUROGATES_IMAGE_ENV_SET``: the pod's value, kept).
    path = environ.get("SUROGATES_IMAGE_ENV_FILE")
    if path:
        try:
            with open(path, encoding="utf-8") as fh:
                declared = json.load(fh)
        except (OSError, ValueError):
            declared = {}
        set_by_pod = {n for n in environ.get("SUROGATES_IMAGE_ENV_SET", "").split(",") if n}
        for name in (declared if isinstance(declared, dict) else ()):
            if is_control(name) and name not in set_by_pod:
                environ.pop(name, None)
    environ["SUROGATES_HOME"] = STATE_DIR
    environ["SUROGATES_CHECKPOINT_DIR"] = f"{STATE_DIR}/checkpoints"


def main() -> None:
    prepare()
    from surogates.sandbox.executor_server import main as serve

    serve()


if __name__ == "__main__":
    main()
