"""Artifacts subsystem — LLM-authored, chat-embedded inline content.

Artifacts are named, versioned, kind-typed blobs (markdown, tables,
Vega-Lite charts) that the LLM creates via the ``create_artifact``
tool.  They live in the session bucket under
``artifacts/{artifact_id}/v{N}.{ext}`` and carry only metadata through
the session event log; the payload is fetched on-demand by the chat
thread.
"""

from surogates.artifacts.models import (
    ArtifactKind,
    ArtifactMeta,
    ArtifactSpec,
    MAX_ARTIFACT_BYTES,
    MAX_ARTIFACTS_PER_SESSION,
)
from surogates.artifacts.store import ArtifactStore

__all__ = [
    "ArtifactKind",
    "ArtifactMeta",
    "ArtifactSpec",
    "ArtifactStore",
    "MAX_ARTIFACT_BYTES",
    "MAX_ARTIFACTS_PER_SESSION",
]
