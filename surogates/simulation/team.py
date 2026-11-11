"""A simulation's team: a stand-in for each of the agent's team members.

Inside a run the agent's real team members its plan names are swapped, for
that run's conversations only, for stand-ins on the run's own account.  An escalation then
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


class InRun:
    """The agent's settings as seen from inside a run: rosters and lookups
    keep to the run's people.  Without stand-ins the team is unchanged, and
    its alerts show in the run as "not sent"."""

    def __init__(self, settings: Any, run_id: uuid.UUID) -> None:
        self._settings = settings
        self.simulation_run_id = run_id

    def __getattr__(self, name: str) -> Any:
        return getattr(self._settings, name)


class RunSettings(InRun):
    """The agent's follow-up settings with its team swapped for a run's stand-ins."""

    def __init__(self, settings: Any, stand_ins: dict[str, str], sender: str,
                 run_id: uuid.UUID, channel: str = "whatsapp") -> None:
        super().__init__(settings, run_id)
        self.alert_recipient_user_ids = list(stand_ins.values())
        self.team_permissions = {
            stand_ins[member]: entry
            for member, entry in (settings.team_permissions or {}).items() if member in stand_ins
        }
        self.team_channels = {stand_in: [channel] for stand_in in stand_ins.values()}
        self.team_destinations: list = []
        self.senders = {channel: sender}
        # The legacy field reads as WhatsApp's sender: elsewhere it would add one.
        self.channel_identifier = sender if channel == "whatsapp" else None


def team_persona(name: str, permissions: dict[str, Any], task: str | None = None) -> str:
    """Who Autopilot plays when it answers for a team member."""
    handles = permissions.get("handles")
    return (
        f"You are {name}, a busy professional on the team behind the business's assistant. "
        "The assistant writes to you when one of its customers needs a person: read what it "
        "sends, open what it offers to show you, and answer it the way you would at work, "
        "briefly, in the language it writes to you in. Tell it what to tell the customer, "
        "or say you will call them yourself."
        + (f" You handle: {handles}." if handles else "")
        + (f" Today you also need something from it yourself, so you write first: {task}. "
           "When it says it can't, accept that and don't push." if task
           else " You never write first.")
    )


async def add_team(
    sf, run: SimulationRun, provision: Any = get_or_create_channel_identity,
    *, members: list[dict[str, Any]],
) -> list[SimulationPerson]:
    """A stand-in, on the run's channel, for each team member a plan line names
    (``{member_id, name, about, test, autopilot}``); they write first with their *about*."""
    from surogates.followup.store import (
        get_settings,
        grant_contact_permission,
        member_permissions,
    )
    from surogates.simulation.store import new_address

    if not members:
        return []
    settings = await get_settings(sf, org_id=run.org_id, agent_id=run.agent_id)
    team = []
    for line in members:
        member = uuid.UUID(str(line["member_id"]))
        task = (line.get("about") or "").strip() or None
        autopilot = bool(line.get("autopilot"))
        address = new_address(run.channel)
        identity = await provision(
            sf, platform=run.channel, platform_user_id=address, org_id=run.org_id,
            display_name=line["name"],
        )
        # The team agreed to be told when they joined it; their stand-in has too.
        await grant_contact_permission(
            sf, org_id=run.org_id, platform=run.channel, platform_user_id=address,
            recorded_by="simulation",
        )
        person = SimulationPerson(
            run_id=run.id, name=line["name"], channel=run.channel, address=address,
            user_id=identity.user_id, stands_for=member, about=line.get("about"),
            test=line.get("test"), task=task,
            persona=team_persona(line["name"], member_permissions(settings, member), task),
            driver="ai" if autopilot else "human", ai_state="talking" if autopilot else None,
        )
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
        return InRun(settings, run.id)
    return RunSettings(settings, {str(m): str(u) for m, u in team}, run.phone_number_id, run.id,
                       run.channel)


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
