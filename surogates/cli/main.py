"""Surogates entrypoint.

Single binary, multiple process types — each K8s deployment runs the same
image with a different subcommand:

    surogate api              Start the API gateway (FastAPI + web SPA)
    surogate worker           Start a harness worker (Redis queue consumer)
    surogate mcp-proxy        Start the MCP proxy service
    surogate migrate          Run database migrations
    surogate doctor <id>      Diagnose why one session is stuck
    surogate org-members      Add, remove or list an organisation's members
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from uuid import UUID


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        stream=sys.stderr,
    )
    # Silence noisy third-party loggers.  The AWS/S3 clients are
    # pathologically chatty at DEBUG (~40 lines per S3 call) and will
    # flood the worker log whenever log_level is DEBUG, so clamp them to
    # WARNING regardless of the root level.
    for name in (
        "uvicorn.access",
        "httpcore",
        "httpx",
        "hpack",
        "openai",
        # Logs every keepalive ping and every forwarded screencast frame at
        # DEBUG — one line per JPEG.
        "websockets",
        "sse_starlette",
        "kubernetes_asyncio",
        "botocore",
        "aiobotocore",
        "boto3",
        "urllib3",
        "s3transfer",
    ):
        logging.getLogger(name).setLevel(logging.WARNING)
    # Uvicorn hands this logger to its websockets protocol, which traces
    # every frame at DEBUG — one line per forwarded screencast JPEG. INFO
    # keeps startup messages and errors.
    logging.getLogger("uvicorn.error").setLevel(logging.INFO)

    # Suppress urllib3's InsecureRequestWarning / connection warnings so
    # they don't spam stderr outside the logging machinery.
    try:
        import urllib3

        urllib3.disable_warnings()
    except Exception:
        pass


# -- subcommands -------------------------------------------------------------


def cmd_api(args: argparse.Namespace) -> None:
    """Start the FastAPI API gateway."""
    import uvicorn

    from surogates.config import load_settings

    settings = load_settings()
    _configure_logging(settings.log_level)

    uvicorn.run(
        "surogates.api.app:create_app",
        factory=True,
        host=settings.api.host,
        port=settings.api.port,
        workers=settings.api.workers,
        # No uvicorn dictConfig: it would re-level uvicorn's loggers over
        # the clamps _configure_logging just set (a DEBUG dev config turns
        # uvicorn.error into a per-frame websocket trace). With no config
        # of its own, uvicorn propagates to the root logging set up above.
        log_config=None,
    )


def cmd_keel_link(args: argparse.Namespace) -> None:
    """Start keel-link, Keel's internal link to the runtime."""
    import uvicorn

    from surogates.config import load_settings

    settings = load_settings()
    _configure_logging(settings.log_level)

    uvicorn.run(
        "surogates.api.keel_link:create_keel_link_app",
        factory=True,
        host=settings.api.host,
        port=settings.keel.link_port,
        # See cmd_api: uvicorn's own dictConfig would override the clamps.
        log_config=None,
    )


def cmd_worker(args: argparse.Namespace) -> None:
    """Start a harness worker that consumes from the Redis work queue."""
    from surogates.config import load_settings

    settings = load_settings()
    _configure_logging(settings.log_level)

    # Default worker_id to hostname (K8s pod name via downward API)
    if not settings.worker_id:
        settings.worker_id = os.environ.get("HOSTNAME", "worker-local")

    logger = logging.getLogger("surogates.worker")
    logger.info(
        "Starting worker %s (concurrency=%d)",
        settings.worker_id,
        settings.worker.concurrency,
    )

    from surogates.orchestrator.worker import run_worker

    asyncio.run(run_worker(settings))


def cmd_mcp_proxy(args: argparse.Namespace) -> None:
    """Start the MCP proxy service."""
    import uvicorn

    from surogates.mcp_proxy.config import load_proxy_settings

    settings = load_proxy_settings()
    _configure_logging(settings.log_level)

    uvicorn.run(
        "surogates.mcp_proxy.app:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        workers=settings.workers,
        # See cmd_api: uvicorn's own dictConfig would override the clamps.
        log_config=None,
    )


def cmd_channels(args: argparse.Namespace) -> None:
    """Start the channel webhook service (inbound + delivery + reconciler)."""
    from surogates.channels.runner import run_channels
    from surogates.config import load_settings

    settings = load_settings()
    _configure_logging(settings.log_level)

    kind: str | None = getattr(args, "kind", None)

    logger = logging.getLogger("surogates.channels")
    logger.info(
        "Starting channel webhook service on port %d%s",
        settings.channels.port,
        f" (kind={kind})" if kind else "",
    )

    asyncio.run(run_channels(settings, kind=kind))


def cmd_voice(args: argparse.Namespace) -> None:
    """Answer phone calls through LiveKit SIP (needs the ``voice`` extra)."""
    from surogates.config import load_settings
    from surogates.voice.worker import run_voice

    settings = load_settings()
    _configure_logging(settings.log_level)
    logging.getLogger("surogates.voice").info(
        "Starting voice worker %r against %s", settings.voice.agent_name, settings.voice.livekit_url,
    )
    asyncio.run(run_voice(settings))


def cmd_migrate(args: argparse.Namespace) -> None:
    """Run database migrations."""
    from surogates.config import load_settings

    settings = load_settings()
    _configure_logging(settings.log_level)

    logger = logging.getLogger("surogates.migrate")
    logger.info("Running migrations against %s", settings.db.url.split("@")[-1])

    from surogates.db.engine import run_migrations

    run_migrations(settings.db)


def cmd_doctor(args: argparse.Namespace) -> None:
    """Report coherence problems for one session."""
    from uuid import UUID

    from surogates.config import load_settings

    settings = load_settings()
    _configure_logging("WARNING")  # findings go to stdout, not the log

    async def _run() -> int:
        from surogates.db.engine import (
            async_engine_from_settings,
            async_session_factory,
        )
        from surogates.session.doctor import diagnose_session
        from surogates.session.store import SessionStore

        engine = async_engine_from_settings(settings.db)
        try:
            findings = await diagnose_session(
                SessionStore(async_session_factory(engine)),
                UUID(args.session_id),
            )
        finally:
            await engine.dispose()
        if not findings:
            print("no findings")
            return 0
        for f in findings:
            print(f"{f.code}: {f.detail}")
        return 1

    sys.exit(asyncio.run(_run()))


#: What each refusal of a membership change says. No sentence carries an email.
_MEMBERSHIP_REFUSALS = {
    "unknown-org": "no organisation has that id",
    "unknown-user": "no user has that id",
    "email-taken": "that email already belongs to another user of the organisation",
    "home-org": "that is the user's home organisation, which they cannot leave",
}


def cmd_org_members(args: argparse.Namespace) -> None:
    """Add a user to an organisation, remove them, or list its users."""
    from sqlalchemy import select

    from surogates.config import load_settings

    settings = load_settings()
    _configure_logging("WARNING")  # answers go to stdout, not the log

    async def _run() -> int:
        from surogates.db.engine import (
            async_engine_from_settings,
            async_session_factory,
        )
        from surogates.db.models import Org, User
        from surogates.tenant.memberships import (
            MembershipError,
            add_member,
            belongs_to,
            remove_member,
        )

        engine = async_engine_from_settings(settings.db)
        try:
            async with async_session_factory(engine)() as db:
                if args.action == "add":
                    added = await add_member(db, args.org, args.user)
                    await db.commit()
                    print("added" if added else "already a member")
                elif args.action == "remove":
                    removed = await remove_member(db, args.org, args.user)
                    await db.commit()
                    print("removed" if removed else "not a member")
                else:
                    if await db.scalar(select(Org.id).where(Org.id == args.org)) is None:
                        raise MembershipError("unknown-org")
                    is_home = User.org_id == args.org
                    rows = await db.execute(
                        select(User.id, User.email, is_home)
                        .where(belongs_to(args.org))
                        .order_by(is_home.desc(), User.email, User.id)
                    )
                    for user_id, email, home in rows:
                        print(f"{user_id} {email} {'home' if home else 'member'}")
        except MembershipError as exc:
            print(f"surogates org-members: {_MEMBERSHIP_REFUSALS[exc.code]}.", file=sys.stderr)
            return 1
        finally:
            await engine.dispose()
        return 0

    code = asyncio.run(_run())
    if code:
        sys.exit(code)


# -- parser ------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="surogates",
        description="Surogates — Managed Agent Platform",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # surogate api
    sub.add_parser("api", help="Start the API gateway")

    # surogate worker
    sub.add_parser("worker", help="Start a harness worker")

    # surogate keel-link
    sub.add_parser("keel-link", help="Start Keel's internal link to the runtime")

    # surogate mcp-proxy
    sub.add_parser("mcp-proxy", help="Start the MCP proxy service")

    # surogate migrate
    sub.add_parser("migrate", help="Run database migrations")

    # surogate doctor <session-id>
    doctor_parser = sub.add_parser(
        "doctor", help="Diagnose why one session is stuck",
    )
    doctor_parser.add_argument("session_id", help="Session UUID")

    # surogate channels [kind]
    channels_parser = sub.add_parser(
        "channels", help="Start the channel webhook service",
    )
    channels_parser.add_argument(
        "kind",
        nargs="?",
        default=None,
        help="Optional platform kind to restrict delivery loops (e.g. 'slack')",
    )

    # surogate voice
    sub.add_parser("voice", help="Answer phone calls through LiveKit SIP")

    # surogate org-members add|remove|list
    members_parser = sub.add_parser(
        "org-members",
        help="Add, remove or list the users of an organisation",
    )
    actions = members_parser.add_subparsers(dest="action", required=True)
    for action, help_text in (
        ("add", "Make a user a member of an organisation"),
        ("remove", "End a user's membership of an organisation"),
    ):
        action_parser = actions.add_parser(action, help=help_text)
        action_parser.add_argument("--org", type=UUID, required=True, help="Organisation UUID")
        action_parser.add_argument("--user", type=UUID, required=True, help="User UUID")
    list_parser = actions.add_parser(
        "list", help="List an organisation's users: home users first, then members",
    )
    list_parser.add_argument("--org", type=UUID, required=True, help="Organisation UUID")

    return parser


COMMANDS = {
    "api": cmd_api,
    "worker": cmd_worker,
    "keel-link": cmd_keel_link,
    "mcp-proxy": cmd_mcp_proxy,
    "migrate": cmd_migrate,
    "doctor": cmd_doctor,
    "channels": cmd_channels,
    "voice": cmd_voice,
    "org-members": cmd_org_members,
}


def cli_main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    COMMANDS[args.command](args)


if __name__ == "__main__":
    cli_main()
