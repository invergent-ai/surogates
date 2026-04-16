"""Surogates entrypoint.

Single binary, multiple process types — each K8s deployment runs the same
image with a different subcommand:

    surogate api              Start the API gateway (FastAPI + web SPA)
    surogate worker           Start a harness worker (Redis queue consumer)
    surogate channel slack    Start the Slack channel adapter
    surogate mcp-proxy        Start the MCP proxy service
    surogate migrate          Run database migrations
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        stream=sys.stderr,
    )
    # Silence noisy third-party loggers.
    for name in (
        "uvicorn.access",
        "httpcore",
        "httpx",
        "hpack",
        "openai",
        "sse_starlette",
        "kubernetes_asyncio"
    ):
        logging.getLogger(name).setLevel(logging.WARNING)


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
        log_level=settings.log_level.lower(),
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


def cmd_channel(args: argparse.Namespace) -> None:
    """Start a channel adapter (slack, teams, telegram, webhook)."""
    from surogates.config import load_settings

    settings = load_settings()
    _configure_logging(settings.log_level)

    channel_type: str = args.type
    logger = logging.getLogger("surogates.channel")
    logger.info("Starting channel adapter: %s", channel_type)

    from surogates.channels import start_channel

    asyncio.run(start_channel(channel_type, settings))


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
        log_level=settings.log_level.lower(),
    )


def cmd_migrate(args: argparse.Namespace) -> None:
    """Run database migrations."""
    from surogates.config import load_settings

    settings = load_settings()
    _configure_logging(settings.log_level)

    logger = logging.getLogger("surogates.migrate")
    logger.info("Running migrations against %s", settings.db.url.split("@")[-1])

    from surogates.db.engine import run_migrations

    run_migrations(settings.db)


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

    # surogate channel <type>
    p_channel = sub.add_parser("channel", help="Start a channel adapter")
    p_channel.add_argument(
        "type",
        choices=["slack", "telegram"],
        help="Channel type to start",
    )

    # surogate mcp-proxy
    sub.add_parser("mcp-proxy", help="Start the MCP proxy service")

    # surogate migrate
    sub.add_parser("migrate", help="Run database migrations")

    return parser


COMMANDS = {
    "api": cmd_api,
    "worker": cmd_worker,
    "channel": cmd_channel,
    "mcp-proxy": cmd_mcp_proxy,
    "migrate": cmd_migrate,
}


def cli_main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    COMMANDS[args.command](args)


if __name__ == "__main__":
    cli_main()
