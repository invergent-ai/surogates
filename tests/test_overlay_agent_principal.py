"""The agent's service account rides on every wake's session, not only one with SSH targets."""

from __future__ import annotations

from types import SimpleNamespace

from surogates.harness.loop import AgentHarness


def harness(agent_sa: str | None, *, ssh_targets=None) -> AgentHarness:
    h = AgentHarness.__new__(AgentHarness)
    h._coding_repos = []
    h._ssh_targets = ssh_targets or []
    h._whatsapp_forms = []
    h._agent_service_account_id = agent_sa
    return h


class Session(SimpleNamespace):
    def model_copy(self, *, update):
        return Session(**{**self.__dict__, **update})


def test_the_agents_service_account_is_stamped_without_ssh_targets() -> None:
    # The browser types only the agent's own credentials for an end user,
    # and finds them under this id.
    session = harness("sa-1")._overlay_repos(Session(config={"execution": {}}))
    assert session.config == {"execution": {}, "agent_service_account_id": "sa-1"}


def test_with_nothing_to_overlay_the_session_is_unchanged() -> None:
    original = Session(config={})
    assert harness(None)._overlay_repos(original) is original
