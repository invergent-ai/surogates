"""ecsim -- the in-sandbox CLI the agent drives the simulation with.

This file is uploaded into the session workspace next to a ``sim/``
directory holding the vendored upstream ``tools/`` and ``data/`` trees
plus ``ecommerce_tool_manager.py``. The agent interacts with the store
exclusively through it:

    python3 ecsim.py init [--max-days N] [--balance X]
    python3 ecsim.py tools
    python3 ecsim.py status
    python3 ecsim.py call '[{"tool_name": "...", "tool_args": {...}}]'
    python3 ecsim.py finalize

State is pickled between invocations (``sim_state.pkl``), so every call
is a fresh process -- no daemon, no ports, nothing to tunnel. Every
batch is also appended to ``calls.jsonl`` for the benchmark's audit.

Only the standard library is imported at module level: the vendored
environment (and its pandas/numpy dependencies) loads lazily inside
commands, so error messages stay actionable for the agent.

NPC dialogue: upstream renders supplier replies with an LLM, but every
negotiation *outcome* comes from the deterministic kernel and is handed
to the renderer as text it must not override. By default this wrapper
installs a template renderer that returns exactly that kernel decision
(zero tokens, zero keys in the sandbox). Setting ``GPT_API_KEY`` (plus
optional ``GPT_BASE_URL`` / ``NPC_MODEL``, upstream's own variables)
restores upstream-faithful LLM rendering.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import pickle
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
SIM_DIR = HERE / "sim"
STATE_PATH = HERE / "sim_state.pkl"
CALLS_LOG = HERE / "calls.jsonl"
FINAL_PATH = HERE / "final_state.json"

_KERNEL_SECTION_RE = re.compile(
    r"## Negotiation Engine Decision[^\n]*\n(.*?)(?=\n## |\Z)", re.DOTALL
)


def render_supplier_reply(messages: list[dict]) -> str:
    """Deterministic supplier reply: surface the kernel's decision.

    The system prompt embeds the negotiation engine's decision under a
    "DO NOT OVERRIDE" header; upstream asks an LLM to phrase it as
    dialogue. This renders it verbatim instead -- same information, same
    outcome, no model call.
    """
    system = next(
        (m.get("content", "") for m in messages if m.get("role") == "system"), ""
    )
    match = _KERNEL_SECTION_RE.search(system)
    decision = (match.group(1).strip() if match else "").strip()
    if decision:
        return (
            "Thanks for your message. Here is our position:\n"
            f"{decision}\n"
            "Let us know how you would like to proceed."
        )
    return (
        "Thanks for your message. We have received it and will reflect "
        "any agreed terms in the order system."
    )


def _install_npc_stub() -> None:
    """Replace the LLM supplier renderer unless upstream's key is set."""
    if os.environ.get("GPT_API_KEY"):
        return
    from tools.opponent import supplier_llm

    supplier_llm._call_supplier_llm = render_supplier_reply
    try:
        from tools import chatbox

        # chatbox binds the symbol by name at import time.
        chatbox._call_supplier_llm = render_supplier_reply
    except Exception:
        pass


def _boot() -> None:
    if not SIM_DIR.is_dir():
        raise SystemExit(f"sim/ directory not found next to {__file__}")
    sys.path.insert(0, str(SIM_DIR))
    os.chdir(SIM_DIR)  # upstream loads data/ via relative paths
    try:
        import pandas  # noqa: F401
        import numpy  # noqa: F401
    except ModuleNotFoundError as exc:
        raise SystemExit(
            f"missing dependency: {exc.name}. "
            "Run: pip install pandas numpy   then retry."
        )
    _install_npc_stub()


def _make_manager():
    """A bare upstream tool manager: tool map attached, no log handles.

    The real ``EcommerceToolManager`` owns open log files, which do not
    pickle; we persist only (env, job) and rebuild this shell per call.
    """
    from tools import ecommerce_tool_map
    from ecommerce_tool_manager import EcommerceToolManager  # type: ignore

    manager = EcommerceToolManager.__new__(EcommerceToolManager)
    manager.env = None
    manager._ecommerce_tool_map = ecommerce_tool_map
    manager._output_log_fp = None
    manager._balance_log_fp = None
    manager._messages_log_fp = None
    manager._seen_balance_dates = set()
    return manager


def _save(env, job) -> None:
    with open(STATE_PATH, "wb") as fh:
        pickle.dump({"env": env, "job": job}, fh)


def _load():
    if not STATE_PATH.exists():
        raise SystemExit("no simulation state -- run: python3 ecsim.py init")
    with open(STATE_PATH, "rb") as fh:
        state = pickle.load(fh)
    return state["env"], state["job"]


def _done(env) -> bool:
    return bool(getattr(env, "is_done", False))


def cmd_init(args) -> None:
    _boot()
    if STATE_PATH.exists() and not args.force:
        raise SystemExit(
            "simulation already initialized (sim_state.pkl exists); "
            "use --force to restart from day 1"
        )
    from tools.ecommerce_env import EcommerceEnv

    # Mirrors upstream EcommerceToolManager.init(job).
    env = EcommerceEnv(
        initial_balance=args.balance,
        store_daily_rent=args.daily_fee,
        max_day=args.max_days,
    )
    job = {
        "agent_info": {
            "initial_balance": args.balance,
            "daily_fee": args.daily_fee,
            "max_day": args.max_days,
            "run_index": 0,
            "context_clear_count": 0,
            "context_tokens_freed_total": 0,
        },
        "messages": [],
        "reward_meta": {},
        "final_day": 1,
    }
    _save(env, job)
    print(json.dumps({
        "ok": True,
        "day": getattr(env, "day_count", 1),
        "max_days": args.max_days,
        "balance": args.balance,
        "note": "Simulation ready. List tools with: python3 ecsim.py tools",
    }))


def cmd_tools(_args) -> None:
    _boot()
    from tools import ecommerce_tool_map

    out = []
    for name, cls in sorted(ecommerce_tool_map.items()):
        # Upstream tools describe themselves in OpenAI function-spec
        # shape; pass that through whole -- it is the agent's manual.
        try:
            info = cls.get_info().get("function", {})
        except Exception:
            info = {}
        out.append({
            "tool_name": name,
            "description": info.get("description")
            or (cls.__doc__ or "").strip(),
            "parameters": info.get("parameters", {}),
        })
    print(json.dumps(out, ensure_ascii=False, indent=1))


def cmd_status(_args) -> None:
    _boot()
    env, job = _load()
    print(json.dumps({
        "day": getattr(env, "day_count", None),
        "time": str(getattr(env, "current_time", "")),
        "max_day": getattr(env, "max_day", None),
        "bank_balance": round(getattr(env, "bank_balance", 0.0), 2),
        "platform_wallet": round(getattr(env, "platform_wallet", 0.0), 2),
        "pending_settlement": round(getattr(env, "pending_settlement", 0.0), 2),
        "done": _done(env),
    }, ensure_ascii=False))


def cmd_call(args) -> None:
    _boot()
    env, job = _load()
    if _done(env):
        raise SystemExit("episode is over -- run: python3 ecsim.py finalize")
    try:
        batch = json.loads(args.batch)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"batch is not valid JSON: {exc}")
    if isinstance(batch, dict):
        batch = [batch]
    if not isinstance(batch, list) or not all(
        isinstance(c, dict) and "tool_name" in c for c in batch
    ):
        raise SystemExit(
            'batch must be [{"tool_name": ..., "tool_args": {...}}, ...]'
        )
    for call in batch:
        call.setdefault("tool_args", {})

    manager = _make_manager()
    manager.env = env
    responses = manager.ask_code_exec(job, batch)
    _save(manager.env, job)

    with open(CALLS_LOG, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "day": getattr(manager.env, "day_count", None),
            "batch": batch,
            "responses": responses,
        }, ensure_ascii=False, default=str) + "\n")

    print(json.dumps(responses, ensure_ascii=False, indent=1))
    if _done(manager.env):
        print(
            "\n[episode ended -- run: python3 ecsim.py finalize]",
            file=sys.stderr,
        )


def cmd_finalize(_args) -> None:
    _boot()
    env, job = _load()
    manager = _make_manager()
    manager.env = env
    final_assets = manager.snapshot_final_state(job)
    try:
        manager.cleanup(job)  # negotiation/fraud analysis into reward_meta
    except Exception as exc:  # noqa: BLE001 - analysis is best-effort
        job.setdefault("reward_meta", {})["cleanup_error"] = repr(exc)
    _save(manager.env, job)

    summary = {
        "final_assets": final_assets,
        "initial_balance": job.get("agent_info", {}).get("initial_balance"),
        "day_count": getattr(env, "day_count", None),
        "final_day": job.get("final_day"),
        "max_day": getattr(env, "max_day", None),
        "bank_balance": round(getattr(env, "bank_balance", 0.0), 2),
        "platform_wallet": round(getattr(env, "platform_wallet", 0.0), 2),
        "pending_settlement": round(getattr(env, "pending_settlement", 0.0), 2),
        "done": _done(env),
        "reward_meta": job.get("reward_meta", {}),
    }
    with open(FINAL_PATH, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=1, default=str)
    print(json.dumps(
        {k: v for k, v in summary.items() if k != "reward_meta"},
        ensure_ascii=False, indent=1,
    ))
    print(f"\nwritten to {FINAL_PATH.name}", file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ecsim")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="Start an episode on day 1")
    init.add_argument("--max-days", type=int, default=365)
    init.add_argument("--balance", type=float, default=100000.0)
    init.add_argument("--daily-fee", type=float, default=50.0)
    init.add_argument("--force", action="store_true")
    init.set_defaults(fn=cmd_init)

    sub.add_parser("tools", help="List available tools").set_defaults(fn=cmd_tools)
    sub.add_parser("status", help="Day, time and balances").set_defaults(fn=cmd_status)

    call = sub.add_parser("call", help="Execute a JSON batch of tool calls")
    call.add_argument("batch")
    call.set_defaults(fn=cmd_call)

    sub.add_parser(
        "finalize", help="Settle the episode and write final_state.json"
    ).set_defaults(fn=cmd_finalize)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.fn(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
