"""``PromptBuilder`` receives the per-agent slash-command config, but the
scheduled-loop *child* guidance (``loop_wait`` / ``cron_loop``) must stay
keyed to session state + loaded tools — NOT to whether ``/loop`` creation
is enabled.  Disabling ``/loop`` prevents creating schedules through the
slash command; it must not strand an already-running scheduled child
without its wait/completion instructions.
"""

from __future__ import annotations

from uuid import UUID

from surogates.harness.prompt import PromptBuilder, default_library
from surogates.runtime import SlashCommandConfig
from surogates.tenant import TenantContext


def _tenant():
    return TenantContext(
        org_id=UUID("00000000-0000-0000-0000-000000000001"),
        user_id=UUID("00000000-0000-0000-0000-000000000002"),
        org_config={},
        user_preferences={},
        permissions=frozenset(),
        asset_root="/tmp/test",
    )


class _Session:
    # Minimal stand-in. PromptBuilder reads ``.config`` for the loop
    # guidance gate and ``.model`` in ``_get_model_id`` (returns early
    # with a real string so the discipline check below it doesn't choke).
    model = "gpt-4o"

    def __init__(self, config):
        self.config = config


def _builder(slash_commands, *, tools=frozenset({"loop_wait"}), config=None):
    return PromptBuilder(
        _tenant(),
        available_tools=set(tools),
        session=_Session(config or {"scheduled_dynamic_loop": True}),
        slash_commands=slash_commands,
    )


def test_loop_guidance_present_when_loop_enabled():
    frag = default_library().get("guidance/loop_wait")
    out = _builder(SlashCommandConfig())._tool_guidance_section()
    assert frag in out


def test_dynamic_loop_child_guidance_stays_present_when_loop_creation_disabled():
    frag = default_library().get("guidance/loop_wait")
    cfg = SlashCommandConfig(enabled=True, commands=frozenset({"clear"}))  # no "loop"
    out = _builder(cfg)._tool_guidance_section()
    assert frag in out


def test_cron_loop_child_guidance_stays_present_when_loop_creation_disabled():
    frag = default_library().get("guidance/cron_loop")
    cfg = SlashCommandConfig(enabled=True, commands=frozenset({"clear"}))  # no "loop"
    out = _builder(
        cfg,
        tools=frozenset({"loop_complete"}),
        config={"scheduled_session_id": "sched-1"},
    )._tool_guidance_section()
    assert frag in out
