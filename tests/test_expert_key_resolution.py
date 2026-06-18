"""Expert calls must carry the per-model proxy credential, not a placeholder.

Experts route through ``/proxy/services/_model/{id}``, which the platform
proxy gates on an ``sk-agent`` key scoped to that model — the same
``vault://byo_model_{id}_key`` minted when the model is served.  These tests
pin the endpoint→model-id parsing and the vault resolution, including the
graceful fallbacks that leave the loop on its existing default.
"""

from __future__ import annotations

import types
import uuid

import pytest

from surogates.tools.builtin.expert_service import (
    _model_id_from_endpoint,
    resolve_expert_api_key,
)
from surogates.tools.loader import SkillDef


def _expert(endpoint: str | None) -> SkillDef:
    return SkillDef(
        name="libra-ytd", description="d", content="", source="x", builtin=False,
        type="expert", category=None, tags=[], platforms=[], fallback_for_tools=[],
        requires_tools=[], trigger=None, expert_model="m", expert_endpoint=endpoint,
        expert_adapter=None, expert_max_iterations=1, expert_status="active",
        expert_tools=[], expert_generation=None, category_description=None,
    )


@pytest.mark.parametrize(
    "endpoint, expected",
    [
        ("/proxy/services/_model/27a20181-aaaa/v1", "27a20181-aaaa"),
        ("/proxy/services/_model/abc", "abc"),
        ("https://host/proxy/services/_model/xyz/v1/chat", "xyz"),
        ("/proxy/services/default/r6b689116", None),  # legacy dstack shape
        (None, None),
    ],
)
def test_model_id_from_endpoint(endpoint, expected):
    assert _model_id_from_endpoint(endpoint) == expected


class _FakeVault:
    def __init__(self) -> None:
        self.asked: dict = {}

    async def resolve_ref(self, ref, *, org_id, user_id=None):
        self.asked = {"ref": ref, "org_id": org_id}
        return "sk-agent-real-for-expert-model"


async def test_resolves_per_model_key_from_vault():
    org = uuid.uuid4()
    vault = _FakeVault()
    key = await resolve_expert_api_key(
        vault, types.SimpleNamespace(org_id=org),
        _expert("/proxy/services/_model/27a20181-aaaa/v1"),
    )
    assert key == "sk-agent-real-for-expert-model"
    assert vault.asked["ref"] == "vault://byo_model_27a20181-aaaa_key"
    assert vault.asked["org_id"] == org


async def test_falls_back_to_none_when_unresolvable():
    org = uuid.uuid4()
    vault = _FakeVault()
    # no vault wired
    assert await resolve_expert_api_key(
        None, types.SimpleNamespace(org_id=org), _expert("/proxy/services/_model/x")
    ) is None
    # legacy / non-_model endpoint
    assert await resolve_expert_api_key(
        vault, types.SimpleNamespace(org_id=org), _expert("/proxy/services/default/run")
    ) is None
    # missing org
    assert await resolve_expert_api_key(
        vault, types.SimpleNamespace(org_id=None), _expert("/proxy/services/_model/x")
    ) is None
