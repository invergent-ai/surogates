"""Source-level regression guard: retired socket-mode and polling adapters.

Asserts that no file under surogates/ (excluding tests/) contains
constructs from the retired socket-mode Slack adapter or polling Telegram
adapter.  If any of these strings reappear in the source tree, CI fails
immediately rather than letting a silent re-introduction slip past code
review.
"""

from __future__ import annotations

import os
from pathlib import Path

# Root of the surogates package (not the tests directory).
_PACKAGE_ROOT = Path(__file__).parent.parent / "surogates"


def _source_files():
    """Yield all .py files under the package root."""
    for dirpath, _dirnames, filenames in os.walk(_PACKAGE_ROOT):
        for name in filenames:
            if name.endswith(".py"):
                yield Path(dirpath) / name


def _combined_source() -> str:
    """Return the concatenated text of all package source files."""
    parts = []
    for path in _source_files():
        try:
            parts.append(path.read_text(encoding="utf-8"))
        except OSError:
            pass
    return "\n".join(parts)


def test_no_async_socket_mode_handler():
    """AsyncSocketModeHandler must not be constructed anywhere in the package.

    The socket-mode runner was the core of the retired SlackAdapter.connect()
    path.  Its presence indicates the old per-process adapter was re-introduced.
    """
    src = _combined_source()
    assert "AsyncSocketModeHandler" not in src, (
        "AsyncSocketModeHandler found in surogates/ source — "
        "the retired socket-mode Slack adapter appears to have been re-introduced."
    )


def test_no_start_polling():
    """start_polling must not appear in the package source.

    The polling runner was the core of the retired TelegramAdapter.connect()
    path.  Its presence indicates the old per-process polling adapter was
    re-introduced.
    """
    src = _combined_source()
    assert "start_polling" not in src, (
        "start_polling found in surogates/ source — "
        "the retired polling Telegram adapter appears to have been re-introduced."
    )


def test_no_start_channel():
    """start_channel must not appear in the package source.

    This function name was used in the retired _ADAPTER_REGISTRY pattern
    that instantiated per-process adapters from process-wide env tokens.
    """
    src = _combined_source()
    assert "start_channel" not in src, (
        "start_channel found in surogates/ source — "
        "the retired adapter registry pattern appears to have been re-introduced."
    )


def test_no_adapter_registry():
    """_ADAPTER_REGISTRY must not appear in the package source."""
    src = _combined_source()
    assert "_ADAPTER_REGISTRY" not in src, (
        "_ADAPTER_REGISTRY found in surogates/ source — "
        "the retired adapter registry appears to have been re-introduced."
    )


def test_no_process_wide_slack_tokens():
    """SUROGATES_SLACK_BOT_TOKEN must not be used to construct an adapter.

    The retired SlackAdapter read per-process tokens from the environment.
    The webhook dispatcher uses per-tenant vault credentials instead.
    """
    src = _combined_source()
    assert "SUROGATES_SLACK_BOT_TOKEN" not in src, (
        "SUROGATES_SLACK_BOT_TOKEN found in surogates/ source — "
        "the retired process-wide Slack token pattern appears to have been re-introduced."
    )


def test_no_process_wide_telegram_tokens():
    """SUROGATES_TELEGRAM_BOT_TOKEN must not be used to construct an adapter.

    The retired TelegramAdapter read per-process tokens from the environment.
    The webhook dispatcher uses per-tenant vault credentials instead.
    """
    src = _combined_source()
    assert "SUROGATES_TELEGRAM_BOT_TOKEN" not in src, (
        "SUROGATES_TELEGRAM_BOT_TOKEN found in surogates/ source — "
        "the retired process-wide Telegram token pattern appears to have been re-introduced."
    )
