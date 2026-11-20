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

import os

#: The executor's own state in a session's own image.
STATE_DIR = "/tmp/.surogates"


def prepare(environ: dict | os._Environ = os.environ) -> None:
    for name in environ.get("SUROGATES_IMAGE_ENV_UNSET", "").split(","):
        if name:
            environ.pop(name, None)
    environ["SUROGATES_HOME"] = STATE_DIR
    environ["SUROGATES_CHECKPOINT_DIR"] = f"{STATE_DIR}/checkpoints"


def main() -> None:
    prepare()
    from surogates.sandbox.executor_server import main as serve

    serve()


if __name__ == "__main__":
    main()
