"""Parse and seed the coding-crew AgentDefs.

The canonical source is the bundled AGENT.md files under
``surogates/environments/coding-crew/<name>/AGENT.md``.  ``parse_crew_agentdefs``
turns them into :class:`AgentDef` objects; ``seed_coding_crew`` upserts them as
``agents`` rows for an org so a ``code-orchestrator`` session can resolve
``claude-coder`` / ``codex-reviewer`` by ``agent_type``.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

from surogates.tools.loader import (
    AgentDef,
    _build_agent_def,
    _parse_agent_frontmatter,
)

CREW_DIR = Path(__file__).resolve().parent.parent / "environments" / "coding-crew"


def parse_crew_agentdefs(crew_dir: Path | None = None) -> list[AgentDef]:
    """Parse every ``<name>/AGENT.md`` under the crew dir into an AgentDef."""
    base = crew_dir or CREW_DIR
    defs: list[AgentDef] = []
    for agent_md in sorted(base.glob("*/AGENT.md")):
        text = agent_md.read_text()
        parsed = _parse_agent_frontmatter(text, agent_md.parent.name)
        defs.append(_build_agent_def(parsed, source="environment"))
    return defs


async def seed_coding_crew(
    session_factory,
    org_id: UUID,
    user_id: UUID | None = None,
    *,
    crew_dir: Path | None = None,
) -> list[str]:
    """Upsert the crew AgentDefs as ``agents`` rows for *org_id*.

    Idempotent on ``(org_id, user_id, name)``: an existing row is updated.
    Returns the list of seeded agent names.
    """
    from sqlalchemy import select

    from surogates.db.models import Agent

    defs = parse_crew_agentdefs(crew_dir)
    names: list[str] = []
    async with session_factory() as session:
        async with session.begin():
            for d in defs:
                config = {
                    k: v
                    for k, v in {
                        "tools": d.tools,
                        "disallowed_tools": d.disallowed_tools,
                        "model": d.model,
                        "max_iterations": d.max_iterations,
                    }.items()
                    if v is not None
                }
                stmt = select(Agent).where(
                    Agent.org_id == org_id,
                    Agent.name == d.name,
                )
                if user_id is None:
                    stmt = stmt.where(Agent.user_id.is_(None))
                else:
                    stmt = stmt.where(Agent.user_id == user_id)
                existing = (await session.execute(stmt)).scalar_one_or_none()

                if existing is None:
                    session.add(
                        Agent(
                            org_id=org_id,
                            user_id=user_id,
                            name=d.name,
                            description=d.description,
                            system_prompt=d.system_prompt,
                            config=config,
                            enabled=True,
                        )
                    )
                else:
                    existing.description = d.description
                    existing.system_prompt = d.system_prompt
                    existing.config = config
                    existing.enabled = True
                names.append(d.name)
    return names


def main(argv: list[str] | None = None) -> int:
    """CLI: ``python -m surogates.coding_agents.crew_seed --org <uuid>``."""
    import argparse
    import asyncio

    from surogates.config import load_settings
    from surogates.db.engine import (
        async_engine_from_settings,
        async_session_factory,
    )

    parser = argparse.ArgumentParser(
        description="Seed the coding-crew AgentDefs (claude-coder, "
        "codex-reviewer, code-orchestrator) for an org.",
    )
    parser.add_argument(
        "--org", required=True, help="org_id (UUID) to seed the crew for",
    )
    parser.add_argument(
        "--user", default=None, help="optional user_id (UUID) for user-scoped seeding",
    )
    args = parser.parse_args(argv)

    settings = load_settings()
    engine = async_engine_from_settings(settings.db)
    factory = async_session_factory(engine)

    async def _run() -> list[str]:
        try:
            return await seed_coding_crew(
                factory,
                UUID(args.org),
                UUID(args.user) if args.user else None,
            )
        finally:
            await engine.dispose()

    names = asyncio.run(_run())
    print("Seeded coding crew:", ", ".join(names))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
