from surogates.harness.slash_skill import parse_slash_command
from surogates.scheduled.schedule import parse_loop_command


def test_loop_is_not_treated_as_skill() -> None:
    assert parse_slash_command("/loop 5m check deploy") is None


def test_loop_command_parser_supports_slash_prompt() -> None:
    parsed = parse_loop_command("5m /babysit-prs")
    assert parsed.interval == "5m"
    assert parsed.prompt == "/babysit-prs"
