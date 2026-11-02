"""A simulation's team: a stand-in for each of the agent's team members.

Inside a WhatsApp run the agent's real team is swapped, for that run's
conversations only, for stand-ins on the run's own number.  An escalation then
reaches the stand-in's phone in the run, the stand-in answers like the team
member would, and the agent's team tools work on that answer as on a real one.
Nothing outside the run sees a stand-in: every other reader of the settings
still gets the real team, and :func:`queue_outreach` keeps their messages
about a simulated person in the run.
"""

from __future__ import annotations

import uuid
from typing import Any

import sqlalchemy as sa

from surogates.channels.identity import get_or_create_channel_identity
from surogates.db.models import SimulationPerson, SimulationRun, User
from surogates.simulation.ids import new_wa_id


class RunSettings:
    """The agent's follow-up settings with its team swapped for a run's stand-ins."""

    def __init__(self, settings: Any, stand_ins: dict[str, str], sender: str,
                 run_id: uuid.UUID | None = None) -> None:
        self._settings = settings
        self.simulation_run_id = run_id
        self._swap = stand_ins
        self.alert_recipient_user_ids = list(stand_ins.values())
        self.team_permissions = {
            stand_ins[member]: entry
            for member, entry in (settings.team_permissions or {}).items() if member in stand_ins
        }
        self.team_channels = {stand_in: ["whatsapp"] for stand_in in stand_ins.values()}
        self.team_destinations: list = []
        self.senders = {"whatsapp": sender}
        self.channel_identifier = sender

    def __getattr__(self, name: str) -> Any:
        return getattr(self._settings, name)


def team_persona(name: str, permissions: dict[str, Any]) -> str:
    """Who Autopilot plays when it answers for a team member."""
    handles = permissions.get("handles")
    return (
        f"You are {name}, a busy professional on the team behind the business's assistant. "
        "The assistant writes to you when one of its customers needs a person: read what it "
        "sends, open what it offers to show you, and answer it the way you would at work, "
        "briefly, in the language it writes to you in. Tell it what to tell the customer, "
        "or say you will call them yourself. You never write first."
        + (f" You handle: {handles}." if handles else "")
    )


async def add_team(
    sf, run: SimulationRun, provision: Any = get_or_create_channel_identity,
    *, autopilot: bool = False,
) -> list[SimulationPerson]:
    """A stand-in for each of the agent's team members, on a WhatsApp run."""
    from surogates.followup.store import (
        get_settings,
        grant_contact_permission,
        member_permissions,
    )

    if run.channel != "whatsapp":
        return []
    settings = await get_settings(sf, org_id=run.org_id, agent_id=run.agent_id)
    members = [uuid.UUID(str(m)) for m in (settings.alert_recipient_user_ids or [])] if settings else []
    if not members:
        return []
    async with sf() as db:
        names = dict((await db.execute(
            sa.select(User.id, User.display_name).where(User.id.in_(members))
        )).all())
    team = []
    for member in members:
        name = names.get(member) or "Team member"
        address = new_wa_id()
        identity = await provision(
            sf, platform="whatsapp", platform_user_id=address, org_id=run.org_id,
            display_name=name,
        )
        # The team agreed to be told when they joined it; their stand-in has too.
        await grant_contact_permission(
            sf, org_id=run.org_id, platform="whatsapp", platform_user_id=address,
            recorded_by="simulation",
        )
        person = SimulationPerson(run_id=run.id, name=name, channel="whatsapp", address=address,
                                  user_id=identity.user_id, stands_for=member,
                                  persona=team_persona(name, member_permissions(settings, member)),
                                  driver="ai" if autopilot else "human",
                                  ai_state="talking" if autopilot else None)
        async with sf() as db:
            await db.execute(sa.update(User).where(User.id == identity.user_id)
                             .values(simulation_run_id=run.id))
            db.add(person)
            await db.commit()
        team.append(person)
    return team


async def run_settings(sf, settings: Any, user_id: Any) -> Any:
    """*settings* as a conversation with *user_id* should see them: inside a
    simulation with a team, with the team swapped for the run's stand-ins."""
    if settings is None or user_id is None:
        return settings
    async with sf() as db:
        run_id = await db.scalar(sa.select(User.simulation_run_id).where(User.id == user_id))
        if run_id is None:
            return settings
        run = await db.get(SimulationRun, run_id)
        if run is None:
            return settings
        team = (await db.execute(
            sa.select(SimulationPerson.stands_for, SimulationPerson.user_id)
            .where(SimulationPerson.run_id == run.id)
            .where(SimulationPerson.stands_for.isnot(None))
        )).all()
    if not team:
        return settings
    return RunSettings(settings, {str(m): str(u) for m, u in team}, run.phone_number_id, run.id)


async def in_scope(sf, settings: Any, user_ids: set) -> set:
    """Of *user_ids*, the people a team with *settings* may see: a run's
    stand-ins see only that run's people, the real team never a simulated one."""
    if not user_ids:
        return set()
    run_id = getattr(settings, "simulation_run_id", None)
    async with sf() as db:
        return set((await db.scalars(
            sa.select(User.id).where(User.id.in_(user_ids))
            .where(User.simulation_run_id == run_id if run_id is not None
                   else User.simulation_run_id.is_(None))
        )).all())
