"""A session's own sandbox image: the image of the task it works on.

A session created with ``config.sandbox_image`` gets a sandbox that runs
that image (a repository with its toolchain and dependencies in place, such
as a coding task's) instead of the backend's own sandbox image.  The tool
executor is not in such an image: the backend mounts the executor bundle
(``images/executor-bundle``) in it read-only and starts the executor from
there, so the model works in the image with the harness's own tools.

``config.sandbox_workdir`` names the folder inside the image that is the
session's workspace (the task's repository: ``/app``, ``/testbed``);
without it the image's own working directory is.

Which images a session may name is the operator's: ``sandbox.allowed_images``
(fnmatch patterns).  Empty, no session may name one, and the harness is what
it was before the feature.  The worker checks again where it builds the
sandbox's spec, so a config that reached a session some other way than the
create route is held to the same list.
"""

from __future__ import annotations

import fnmatch
import posixpath
import re
from dataclasses import dataclass
from typing import Any

#: The session config keys, set at creation; a sub-agent inherits both from its parent, whose sandbox it shares.
IMAGE_KEY = "sandbox_image"
WORKDIR_KEY = "sandbox_workdir"
SANDBOX_SHARING_FIELDS = (IMAGE_KEY, WORKDIR_KEY)

#: Where the executor bundle is mounted in a session's image, and what the executor runs there.
BUNDLE_MOUNT = "/opt/surogates"
BUNDLE_TINI = f"{BUNDLE_MOUNT}/bin/tini"
BUNDLE_PYTHON = f"{BUNDLE_MOUNT}/bin/python3"

#: An image reference as Docker reads one: [registry[:port]/]name[/name...][:tag][@sha256:digest].  Nothing else
#: reaches ``docker run``'s argument list, so no reference can be read there as a flag.
_COMPONENT = r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*"
_LABEL = r"[a-zA-Z0-9](?:[a-zA-Z0-9-]*[a-zA-Z0-9])?"
# A first part is a registry only with a dot or a port in it, or as localhost; otherwise it is a lowercase name.
_REFERENCE_RE = re.compile(
    rf"(?:(?:localhost(?::[0-9]{{1,5}})?|{_LABEL}(?:\.{_LABEL})+(?::[0-9]{{1,5}})?|{_LABEL}:[0-9]{{1,5}})/)?"
    rf"{_COMPONENT}(?:/{_COMPONENT})*"
    r"(?::[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})?"
    r"(?:@sha256:[0-9a-f]{64})?"
)
_MAX_REFERENCE = 255
#: A workspace folder: absolute, plain characters only (it reaches ``--workdir`` and the executor's environment).
_WORKDIR_RE = re.compile(r"^/[A-Za-z0-9._@+-]+(?:/[A-Za-z0-9._@+-]+)*$")
_MAX_WORKDIR = 1024


class SessionImageRefused(ValueError):
    """A session's ``sandbox_image`` / ``sandbox_workdir`` that may not run; the message says why."""


@dataclass(frozen=True, slots=True)
class SessionImage:
    """The image a session's sandbox runs, and its workspace folder there ("" = the image's own working directory)."""

    image: str
    workdir: str = ""


#: A name Docker could also read as an image ID or an ID's prefix (``docker run 3f2a9c...``): never a session's image,
#: which must be named.
_ID_LIKE_RE = re.compile(r"(?:sha256:)?[0-9a-f]{6,64}")


def valid_reference(image: str) -> bool:
    """Whether *image* is an image reference as Docker reads one, names an image (not an ID), and nothing more."""
    return (
        len(image) <= _MAX_REFERENCE and _REFERENCE_RE.fullmatch(image) is not None
        and _ID_LIKE_RE.fullmatch(image) is None
    )


def normalized(image: str) -> str:
    """*image* as Docker names it in full: ``ubuntu:24.04`` is ``docker.io/library/ubuntu:24.04``."""
    first, _, rest = image.partition("/")
    if not rest or ("." not in first and ":" not in first and first != "localhost"):
        image = f"docker.io/{image if rest else 'library/' + image}"
    return image


def valid_workdir(workdir: str) -> bool:
    """Whether *workdir* is an absolute folder below ``/``, of plain characters, with no ``.`` or ``..`` step."""
    if len(workdir) > _MAX_WORKDIR or _WORKDIR_RE.fullmatch(workdir) is None:
        return False
    return posixpath.normpath(workdir) == workdir and all(part not in (".", "..") for part in workdir.split("/"))


def allowed(image: str, patterns: list[str]) -> bool:
    """Whether *image*, as written or in full (:func:`normalized`), matches one of the operator's *patterns*
    (fnmatch, case kept)."""
    names = {image, normalized(image)}
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns if pattern for name in names)


def names_image(config: dict | None) -> bool:
    """Whether *config* says anything of a session image: only then is there anything to check."""
    return bool(config) and (IMAGE_KEY in config or WORKDIR_KEY in config)


def session_image_of(config: dict | None, sandbox_settings: Any) -> SessionImage | None:
    """The image *config* asks its sandbox to run, or ``None`` for the backend's own.

    Raises :class:`SessionImageRefused` when it names one that may not run:
    not a reference, not on ``sandbox_settings.allowed_images``, without a
    digest where ``require_image_digest`` asks for one, or with a workspace
    folder that is no plain absolute path.
    """
    config = config or {}
    image = config.get(IMAGE_KEY)
    workdir = config.get(WORKDIR_KEY)
    if image is None or image == "":
        if workdir not in (None, ""):
            raise SessionImageRefused(f"{WORKDIR_KEY} is a folder in the session's own image: it needs {IMAGE_KEY}")
        return None
    if not isinstance(image, str) or not valid_reference(image):
        raise SessionImageRefused(f"{IMAGE_KEY} is not an image reference such as registry/name:tag")
    if workdir is None:
        workdir = ""
    if not isinstance(workdir, str) or (workdir and not valid_workdir(workdir)):
        raise SessionImageRefused(f"{WORKDIR_KEY} is not an absolute folder such as /app")
    patterns = list(getattr(sandbox_settings, "allowed_images", None) or [])
    if not patterns:
        raise SessionImageRefused("This deployment runs no session in its own image (sandbox.allowed_images is empty)")
    if not allowed(image, patterns):
        raise SessionImageRefused(f"The image {image} is not one this deployment runs (sandbox.allowed_images)")
    if getattr(sandbox_settings, "require_image_digest", False) and "@sha256:" not in image:
        raise SessionImageRefused(f"The image {image} must be pinned by digest (name@sha256:...)")
    return SessionImage(image=image, workdir=workdir)


def refusal(config: dict | None, sandbox_settings: Any) -> str | None:
    """Why *config*'s session image may not run, or ``None`` when it may (or names none)."""
    try:
        session_image_of(config, sandbox_settings)
    except SessionImageRefused as exc:
        return str(exc)
    return None
