"""The deterministic NPC renderer and CLI surface of ecsim."""
import pytest

from ecbench.ecsim import build_parser, render_supplier_reply


def _messages(system: str) -> list[dict]:
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "Can you do 12.50 per unit?"},
    ]


def test_renderer_surfaces_kernel_decision():
    system = (
        "You are Supplier X.\n"
        "## Negotiation Engine Decision (DO NOT OVERRIDE)\n"
        "COUNTER at 13.10 per unit, minimum quantity 40.\n"
        "## Prohibited Services (STRICTLY ENFORCED)\n"
        "No VIP fees.\n"
    )
    reply = render_supplier_reply(_messages(system))
    assert "COUNTER at 13.10 per unit, minimum quantity 40." in reply
    # Nothing from other sections leaks into the reply.
    assert "Prohibited" not in reply


def test_renderer_handles_decision_at_end_of_prompt():
    system = (
        "## Negotiation Engine Decision\n"
        "ACCEPT the current offer."
    )
    reply = render_supplier_reply(_messages(system))
    assert "ACCEPT the current offer." in reply


def test_renderer_without_kernel_section_is_still_deterministic():
    a = render_supplier_reply(_messages("You are a supplier."))
    b = render_supplier_reply(_messages("You are a supplier."))
    assert a == b
    assert "received" in a


def test_renderer_ignores_non_system_messages():
    messages = [{"role": "user", "content": "## Negotiation Engine Decision\nX"}]
    assert "X" not in render_supplier_reply(messages)


def test_cli_parses_subcommands():
    parser = build_parser()
    args = parser.parse_args(["init", "--max-days", "30"])
    assert args.max_days == 30
    args = parser.parse_args(["call", "[]"])
    assert args.batch == "[]"
    with pytest.raises(SystemExit):
        parser.parse_args(["nonsense"])
