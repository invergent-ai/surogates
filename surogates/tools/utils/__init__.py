from __future__ import annotations

"""Shared utility modules for Surogates tool implementations.

Re-exports key symbols so callers can use short imports like::

    from surogates.tools.utils import strip_ansi, is_safe_url
"""

from surogates.tools.utils.ansi_strip import strip_ansi
from surogates.tools.utils.budget_config import (
    DEFAULT_PREVIEW_SIZE_CHARS,
    DEFAULT_RESULT_SIZE_CHARS,
    DEFAULT_TURN_BUDGET_CHARS,
    PINNED_THRESHOLDS,
    BudgetConfig,
    DEFAULT_BUDGET,
)
from surogates.tools.utils.tool_result_storage import (
    PERSISTED_OUTPUT_CLOSING_TAG,
    PERSISTED_OUTPUT_TAG,
    STORAGE_DIR,
    enforce_turn_budget,
    generate_preview,
    maybe_persist_tool_result,
)
from surogates.tools.utils.binary_extensions import BINARY_EXTENSIONS, has_binary_extension
from surogates.tools.utils.env_passthrough import (
    clear_env_passthrough,
    get_all_passthrough,
    is_env_passthrough,
    register_env_passthrough,
    reset_config_cache,
)
from surogates.tools.utils.fuzzy_match import fuzzy_find_and_replace
from surogates.tools.utils.patch_parser import (
    Hunk,
    HunkLine,
    OperationType,
    PatchOperation,
    apply_v4a_operations,
    parse_v4a_patch,
)
from surogates.tools.utils.checkpoint_manager import CheckpointManager, format_checkpoint_list
from surogates.tools.utils.url_safety import is_safe_url
from surogates.tools.utils.website_policy import (
    WebsitePolicyError,
    check_website_access,
    invalidate_cache,
    load_website_blocklist,
)

__all__ = [
    # ansi_strip
    "strip_ansi",
    # binary_extensions
    "BINARY_EXTENSIONS",
    "has_binary_extension",
    # env_passthrough
    "clear_env_passthrough",
    "get_all_passthrough",
    "is_env_passthrough",
    "register_env_passthrough",
    "reset_config_cache",
    # fuzzy_match
    "fuzzy_find_and_replace",
    # patch_parser
    "Hunk",
    "HunkLine",
    "OperationType",
    "PatchOperation",
    "apply_v4a_operations",
    "parse_v4a_patch",
    # url_safety
    "is_safe_url",
    # website_policy
    "WebsitePolicyError",
    "check_website_access",
    "invalidate_cache",
    "load_website_blocklist",
    # checkpoint_manager
    "CheckpointManager",
    "format_checkpoint_list",
    # budget_config
    "DEFAULT_PREVIEW_SIZE_CHARS",
    "DEFAULT_RESULT_SIZE_CHARS",
    "DEFAULT_TURN_BUDGET_CHARS",
    "PINNED_THRESHOLDS",
    "BudgetConfig",
    "DEFAULT_BUDGET",
    # tool_result_storage
    "PERSISTED_OUTPUT_CLOSING_TAG",
    "PERSISTED_OUTPUT_TAG",
    "STORAGE_DIR",
    "enforce_turn_budget",
    "generate_preview",
    "maybe_persist_tool_result",
]
