"""Harness API client -- HTTP calls from the worker to the API server.

Used by harness tool handlers (skills, memory) to access tenant-scoped
resources via the trusted API server instead of direct storage access.
The worker pod only has credentials for its own session workspace; all
tenant-level operations go through this client.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class HarnessAPIClient:
    """Async HTTP client for harness → API server communication.

    Parameters
    ----------
    base_url:
        API server base URL (e.g. ``http://localhost:8000``).
    token:
        Session-scoped JWT for authentication.
    timeout:
        Request timeout in seconds.
    session_id:
        The session this client is scoped to.  Forwarded as a
        ``session_id`` query parameter on skill-view endpoints so the API
        server can auto-stage supporting files into the session's
        agent bucket.  Required for staging to take effect; omitted
        means skills view as text only (legacy behaviour).
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        timeout: float = 30.0,
        session_id: str | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )
        self._session_id = session_id

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send a GET request and return the parsed JSON response."""
        resp = await self._client.get(path, params=params)
        resp.raise_for_status()
        return resp.json()

    async def _post(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send a POST request and return the parsed JSON response."""
        resp = await self._client.post(path, json=body)
        resp.raise_for_status()
        return resp.json()

    async def _put(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        """Send a PUT request and return the parsed JSON response."""
        resp = await self._client.put(path, json=body)
        resp.raise_for_status()
        return resp.json()

    async def _patch(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        """Send a PATCH request and return the parsed JSON response."""
        resp = await self._client.patch(path, json=body)
        resp.raise_for_status()
        return resp.json()

    async def _delete(self, path: str, params: dict[str, Any] | None = None) -> None:
        """Send a DELETE request."""
        resp = await self._client.delete(path, params=params)
        resp.raise_for_status()

    # ------------------------------------------------------------------
    # Skills
    # ------------------------------------------------------------------

    async def list_skills(self, category: str | None = None) -> str:
        """List available skills.  Returns JSON string for tool result."""
        params: dict[str, Any] = {}
        if category:
            params["category"] = category
        data = await self._get("/v1/skills", params=params)
        # Reshape to match the existing skills_list handler response.
        skills = data.get("skills", [])
        return json.dumps({
            "success": True,
            "skills": skills,
            "categories": sorted(set(s.get("category") for s in skills if s.get("category"))),
            "count": len(skills),
            "hint": "Use skill_view(name) to see full content, tags, and linked files",
        }, ensure_ascii=False)

    async def view_skill(self, name: str, file_path: str | None = None) -> str:
        """View a skill's content or a linked file.  Returns JSON string.

        Forwards the client's ``session_id`` (if set) to the API so that
        supporting files get auto-staged into the session workspace.
        """
        params: dict[str, Any] = {}
        if self._session_id is not None:
            params["session_id"] = self._session_id
        if file_path:
            params["path"] = file_path
            data = await self._get(f"/v1/skills/{name}/file", params=params)
        else:
            data = await self._get(f"/v1/skills/{name}", params=params or None)
        return json.dumps({"success": True, **data}, ensure_ascii=False)

    async def create_skill(
        self,
        name: str,
        content: str,
        category: str | None = None,
    ) -> str:
        """Create a new skill.  Returns JSON string."""
        body: dict[str, Any] = {"name": name, "content": content}
        if category:
            body["category"] = category
        try:
            data = await self._post("/v1/skills", body=body)
            return json.dumps(data, ensure_ascii=False)
        except httpx.HTTPStatusError as exc:
            return _error_response(exc)

    async def edit_skill(self, name: str, content: str) -> str:
        """Replace a skill's full content.  Returns JSON string."""
        try:
            data = await self._put(f"/v1/skills/{name}", body={"content": content})
            return json.dumps(data, ensure_ascii=False)
        except httpx.HTTPStatusError as exc:
            return _error_response(exc)

    async def patch_skill(
        self,
        name: str,
        old_string: str,
        new_string: str,
        file_path: str | None = None,
        replace_all: bool = False,
    ) -> str:
        """Targeted patch of a skill file.  Returns JSON string."""
        body: dict[str, Any] = {
            "old_string": old_string,
            "new_string": new_string,
            "replace_all": replace_all,
        }
        if file_path:
            body["file_path"] = file_path
        try:
            data = await self._patch(f"/v1/skills/{name}", body=body)
            return json.dumps(data, ensure_ascii=False)
        except httpx.HTTPStatusError as exc:
            return _error_response(exc)

    async def delete_skill(self, name: str) -> str:
        """Delete a skill.  Returns JSON string."""
        try:
            await self._delete(f"/v1/skills/{name}")
            return json.dumps({"success": True, "message": f"Skill '{name}' deleted."}, ensure_ascii=False)
        except httpx.HTTPStatusError as exc:
            return _error_response(exc)

    async def write_skill_file(self, name: str, file_path: str, file_content: str) -> str:
        """Write a supporting file to a skill.  Returns JSON string."""
        try:
            data = await self._post(
                f"/v1/skills/{name}/files",
                body={"file_path": file_path, "file_content": file_content},
            )
            return json.dumps(data, ensure_ascii=False)
        except httpx.HTTPStatusError as exc:
            return _error_response(exc)

    async def remove_skill_file(self, name: str, file_path: str) -> str:
        """Remove a supporting file from a skill.  Returns JSON string."""
        try:
            await self._delete(f"/v1/skills/{name}/files", params={"path": file_path})
            return json.dumps({
                "success": True,
                "message": f"File '{file_path}' removed from skill '{name}'.",
            }, ensure_ascii=False)
        except httpx.HTTPStatusError as exc:
            return _error_response(exc)

    # ------------------------------------------------------------------
    # Memory
    # ------------------------------------------------------------------

    async def get_memory(self) -> str:
        """Load current memory entries.  Returns JSON string."""
        data = await self._get("/v1/memory")
        return json.dumps({"success": True, **data}, ensure_ascii=False)

    async def mutate_memory(
        self,
        action: str,
        target: str,
        content: str | None = None,
        old_text: str | None = None,
    ) -> str:
        """Add, replace, or remove a memory entry.  Returns JSON string."""
        body: dict[str, Any] = {"action": action, "target": target}
        if content is not None:
            body["content"] = content
        if old_text is not None:
            body["old_text"] = old_text
        try:
            data = await self._post("/v1/memory", body=body)
            return json.dumps(data, ensure_ascii=False)
        except httpx.HTTPStatusError as exc:
            return _error_response(exc)

    # ------------------------------------------------------------------
    # Artifacts
    # ------------------------------------------------------------------

    async def create_artifact(
        self,
        *,
        name: str,
        kind: str,
        spec: dict[str, Any],
    ) -> str:
        """Create an artifact in the current session.  Returns JSON string.

        Requires ``session_id`` to be set on the client (set by the
        harness when wiring the per-session client).  The API server
        emits an ``artifact.created`` event as part of the POST, so the
        harness does not need to emit one itself.
        """
        if self._session_id is None:
            return json.dumps({
                "success": False,
                "error": (
                    "Artifacts require a session-scoped API client; "
                    "session_id is not set."
                ),
            }, ensure_ascii=False)
        try:
            data = await self._post(
                f"/v1/sessions/{self._session_id}/artifacts",
                body={"name": name, "kind": kind, "spec": spec},
            )
            return json.dumps({"success": True, **data}, ensure_ascii=False)
        except httpx.HTTPStatusError as exc:
            return _error_response(exc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _error_response(exc: httpx.HTTPStatusError) -> str:
    """Extract error detail from an HTTP error response."""
    try:
        detail = exc.response.json().get("detail", str(exc))
    except Exception:
        detail = str(exc)
    return json.dumps({"success": False, "error": detail}, ensure_ascii=False)
