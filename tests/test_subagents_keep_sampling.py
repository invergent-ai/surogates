"""A sub-agent on its parent's model is asked as its parent is: thinking switched off stays off.

Before, ``create_child_session`` built the child's config without the
parent's sampling settings: a session made with ``enable_thinking: false``
(the 11-20 thinking-off reads) had its sub-agents think at full length; 36 %
of those sessions' completion tokens came from the parts that kept thinking on.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from surogates.harness.llm_call import sampling_kwargs
from surogates.session.provisioning import create_child_session
from surogates.tools.builtin.delegate import child_tool_policy

SAMPLING = {"enable_thinking": False, "reasoning_effort": "low", "temperature": 0.2, "max_tokens": 4000}


class _Store:
    async def create_session(self, **kwargs):
        return SimpleNamespace(**kwargs)


def _parent(**config):
    return SimpleNamespace(
        id=uuid4(), org_id=uuid4(), user_id=uuid4(), agent_id="a-1", model="surogate/Surogate3.7-35B-A3B",
        service_account_id=None, channel="api",
        config={"storage_bucket": "b", "storage_key_prefix": "p", "workspace_path": "/workspace", **config},
    )


async def test_a_child_on_the_parents_model_is_asked_as_the_parent_is():
    parent = _parent(**SAMPLING)
    child = await create_child_session(store=_Store(), parent=parent, channel="delegation", config={"max_iterations": 30})
    assert {key: child.config.get(key) for key in SAMPLING} == SAMPLING
    # Its requests carry the switch: no thinking in the sub-agent of a session that has none.
    assert sampling_kwargs(child.config)["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}
    same = await create_child_session(store=_Store(), parent=parent, channel="delegation", model=parent.model)
    assert {key: same.config.get(key) for key in SAMPLING} == SAMPLING


async def test_a_child_on_another_model_takes_none_of_it():
    """A chat-template switch, or an OpenAI field, one endpoint takes and another refuses."""
    child = await create_child_session(store=_Store(), parent=_parent(**SAMPLING), channel="delegation", model="gpt-5")
    assert not set(SAMPLING) & set(child.config)
    assert sampling_kwargs(child.config) == {}


async def test_what_the_code_making_the_child_sets_wins():
    child = await create_child_session(
        store=_Store(), parent=_parent(**SAMPLING), channel="delegation", config={"temperature": 0.9, "enable_thinking": True},
    )
    assert child.config["temperature"] == 0.9 and child.config["enable_thinking"] is True
    assert child.config["reasoning_effort"] == "low" and child.config["max_tokens"] == 4000


async def test_a_parent_that_sets_nothing_gives_nothing():
    child = await create_child_session(store=_Store(), parent=_parent(), channel="delegation")
    assert not set(SAMPLING) & set(child.config)


def test_the_parents_tool_exclusions_already_reach_the_child():
    """Checked, not changed: the delegate's tool policy inherits excluded_tools and allowed_tools."""
    allowed, excluded = child_tool_policy(
        parent_config={"excluded_tools": ["consult_expert", "terminal"]}, agent_def=None, role="leaf", leaf_tools=("agent",),
    )
    assert allowed is None and {"consult_expert", "terminal", "agent"} <= set(excluded)
    allowed, _ = child_tool_policy(
        parent_config={"allowed_tools": ["read_file", "agent"]}, agent_def=None, role="leaf", leaf_tools=("agent",),
    )
    assert allowed == ["read_file"]


async def test_before_the_parents_first_wake_its_child_on_the_same_default_model_inherits():
    parent = _parent(**SAMPLING)
    parent.model = None
    child = await create_child_session(store=_Store(), parent=parent, channel="delegation")
    assert child.config["enable_thinking"] is False


async def test_a_scheduled_run_keeps_no_package_so_it_takes_none_of_it():
    """Its model is picked at wake by the agent's own tier, not the chat's package: maybe another endpoint."""
    parent = _parent(**SAMPLING, entitlements={"model_tier": "pro"})
    run = await create_child_session(store=_Store(), parent=parent, channel="scheduled")
    assert not set(SAMPLING) & set(run.config)
    helper = await create_child_session(store=_Store(), parent=parent, channel="delegation")
    assert helper.config["enable_thinking"] is False  # a helper keeps the package, and so the tier


async def test_a_child_on_another_tier_takes_none_of_it():
    parent = _parent(**SAMPLING, workstream_tier="standard")
    child = await create_child_session(store=_Store(), parent=parent, channel="delegation", config={"workstream_tier": "pro"})
    assert not set(SAMPLING) & set(child.config)

