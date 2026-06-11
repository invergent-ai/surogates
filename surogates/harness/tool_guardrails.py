"""Pure per-turn tool-call loop guardrails."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Mapping

IDEMPOTENT_TOOL_NAMES = frozenset({
    "file_read",
    "read_file",
    "search_files",
    "list_files",
    "web_search",
    "web_extract",
    "web_crawl",
    "session_search",
    "skills_list",
    "skill_view",
})

MUTATING_TOOL_NAMES = frozenset({
    "terminal",
    "execute_code",
    "write_file",
    "patch",
    "memory",
    "skill_manage",
    "browser_click",
    "browser_type",
    "browser_press",
    "browser_scroll",
    "browser_navigate",
    "send_message",
    "delegate_task",
})


@dataclass(frozen=True)
class ToolGuardrailConfig:
    """Thresholds for repeated failed or non-progressing tool calls."""

    warnings_enabled: bool = True
    hard_stop_enabled: bool = False
    exact_failure_warn_after: int = 2
    exact_failure_block_after: int = 5
    same_tool_failure_warn_after: int = 3
    same_tool_failure_halt_after: int = 8
    no_progress_warn_after: int = 2
    no_progress_block_after: int = 5
    # Consecutive identical call + identical result, any tool (mutating
    # included). Enabled independently of ``hard_stop_enabled``: providers
    # reject conversations whose history accumulates identical consecutive
    # tool calls, so the harness must break the pattern before they do.
    consecutive_no_progress_enabled: bool = True
    consecutive_no_progress_warn_after: int = 2
    consecutive_no_progress_block_after: int = 3
    idempotent_tools: frozenset[str] = field(default_factory=lambda: IDEMPOTENT_TOOL_NAMES)
    mutating_tools: frozenset[str] = field(default_factory=lambda: MUTATING_TOOL_NAMES)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> "ToolGuardrailConfig":
        if not isinstance(data, Mapping):
            return cls()
        warn_after = data.get("warn_after")
        if not isinstance(warn_after, Mapping):
            warn_after = {}
        hard_stop_after = data.get("hard_stop_after")
        if not isinstance(hard_stop_after, Mapping):
            hard_stop_after = {}
        defaults = cls()
        return cls(
            warnings_enabled=_as_bool(data.get("warnings_enabled"), defaults.warnings_enabled),
            hard_stop_enabled=_as_bool(data.get("hard_stop_enabled"), defaults.hard_stop_enabled),
            exact_failure_warn_after=_positive_int(
                warn_after.get("exact_failure", data.get("exact_failure_warn_after")),
                defaults.exact_failure_warn_after,
            ),
            exact_failure_block_after=_positive_int(
                hard_stop_after.get("exact_failure", data.get("exact_failure_block_after")),
                defaults.exact_failure_block_after,
            ),
            same_tool_failure_warn_after=_positive_int(
                warn_after.get("same_tool_failure", data.get("same_tool_failure_warn_after")),
                defaults.same_tool_failure_warn_after,
            ),
            same_tool_failure_halt_after=_positive_int(
                hard_stop_after.get("same_tool_failure", data.get("same_tool_failure_halt_after")),
                defaults.same_tool_failure_halt_after,
            ),
            no_progress_warn_after=_positive_int(
                warn_after.get("idempotent_no_progress", data.get("no_progress_warn_after")),
                defaults.no_progress_warn_after,
            ),
            no_progress_block_after=_positive_int(
                hard_stop_after.get("idempotent_no_progress", data.get("no_progress_block_after")),
                defaults.no_progress_block_after,
            ),
            consecutive_no_progress_enabled=_as_bool(
                data.get("consecutive_no_progress_enabled"),
                defaults.consecutive_no_progress_enabled,
            ),
            consecutive_no_progress_warn_after=_positive_int(
                warn_after.get("consecutive_no_progress", data.get("consecutive_no_progress_warn_after")),
                defaults.consecutive_no_progress_warn_after,
            ),
            consecutive_no_progress_block_after=_positive_int(
                hard_stop_after.get("consecutive_no_progress", data.get("consecutive_no_progress_block_after")),
                defaults.consecutive_no_progress_block_after,
            ),
        )


@dataclass(frozen=True)
class ToolCallSignature:
    """Stable identity for a tool name plus canonical argument hash."""

    tool_name: str
    args_hash: str

    @classmethod
    def from_call(
        cls,
        tool_name: str,
        args: Mapping[str, Any] | None,
    ) -> "ToolCallSignature":
        return cls(tool_name=tool_name, args_hash=_sha256(canonical_tool_args(args or {})))

    def to_metadata(self) -> dict[str, str]:
        return {"tool_name": self.tool_name, "args_hash": self.args_hash}


@dataclass(frozen=True)
class ToolGuardrailDecision:
    """Decision returned by :class:`ToolGuardrails`."""

    action: str = "allow"
    code: str = "allow"
    message: str = ""
    tool_name: str = ""
    count: int = 0
    signature: ToolCallSignature | None = None

    @property
    def allows_execution(self) -> bool:
        return self.action in {"allow", "warn"}

    @property
    def should_halt(self) -> bool:
        return self.action in {"block", "halt"}

    def to_metadata(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "action": self.action,
            "code": self.code,
            "message": self.message,
            "tool_name": self.tool_name,
            "count": self.count,
        }
        if self.signature is not None:
            data["signature"] = self.signature.to_metadata()
        return data


def canonical_tool_args(args: Mapping[str, Any]) -> str:
    """Return sorted compact JSON for parsed tool arguments."""
    if not isinstance(args, Mapping):
        raise TypeError(f"tool args must be a mapping, got {type(args).__name__}")
    return json.dumps(args, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def classify_tool_failure(tool_name: str, result: str | None) -> bool:
    if result is None:
        return False
    parsed = _safe_json_loads(result)
    if tool_name == "terminal" and isinstance(parsed, dict):
        exit_code = parsed.get("exit_code")
        return exit_code is not None and exit_code != 0
    if isinstance(parsed, dict):
        if parsed.get("error") or parsed.get("failed") is True:
            return True
    lower = result[:500].lower()
    return '"error"' in lower or '"failed"' in lower or result.startswith("Error")


class ToolGuardrails:
    """Per-turn controller for repeated tool failures and no-progress reads."""

    def __init__(self, config: ToolGuardrailConfig | None = None) -> None:
        self.config = config or ToolGuardrailConfig()
        self._exact_failure_counts: dict[ToolCallSignature, int] = {}
        self._same_tool_failure_counts: dict[str, int] = {}
        self._no_progress: dict[ToolCallSignature, tuple[str, int]] = {}
        self._halt_decision: ToolGuardrailDecision | None = None
        # Consecutive no-progress chain: the latest signature, the result
        # hash the chain is stuck on, and how many completed identical
        # rounds it has. Any differing call or result restarts the chain.
        self._consecutive_signature: ToolCallSignature | None = None
        self._consecutive_result_hash: str | None = None
        self._consecutive_count: int = 0

    @property
    def halt_decision(self) -> ToolGuardrailDecision | None:
        return self._halt_decision

    def before_call(
        self,
        tool_name: str,
        args: Mapping[str, Any] | None,
    ) -> ToolGuardrailDecision:
        signature = ToolCallSignature.from_call(tool_name, _coerce_args(args))

        if self.config.consecutive_no_progress_enabled:
            if signature != self._consecutive_signature:
                self._consecutive_signature = signature
                self._consecutive_result_hash = None
                self._consecutive_count = 0
            elif self._consecutive_count >= self.config.consecutive_no_progress_block_after:
                decision = ToolGuardrailDecision(
                    action="block",
                    code="consecutive_no_progress_block",
                    message=(
                        f"Blocked {tool_name}: this exact call already ran "
                        f"{self._consecutive_count} times in a row with the same "
                        "result. Repeating it cannot make progress and model "
                        "providers reject conversations with repeated identical "
                        "tool calls. Change the arguments, use a different tool, "
                        "or explain the blocker."
                    ),
                    tool_name=tool_name,
                    count=self._consecutive_count,
                    signature=signature,
                )
                self._halt_decision = decision
                return decision

        if not self.config.hard_stop_enabled:
            return ToolGuardrailDecision(tool_name=tool_name, signature=signature)

        exact_count = self._exact_failure_counts.get(signature, 0)
        if exact_count >= self.config.exact_failure_block_after:
            decision = ToolGuardrailDecision(
                action="block",
                code="repeated_exact_failure_block",
                message=(
                    f"Blocked {tool_name}: the same tool call failed {exact_count} "
                    "times with identical arguments. Change strategy or explain the blocker."
                ),
                tool_name=tool_name,
                count=exact_count,
                signature=signature,
            )
            self._halt_decision = decision
            return decision

        if self._is_idempotent(tool_name):
            previous = self._no_progress.get(signature)
            if previous is not None and previous[1] >= self.config.no_progress_block_after:
                decision = ToolGuardrailDecision(
                    action="block",
                    code="idempotent_no_progress_block",
                    message=(
                        f"Blocked {tool_name}: this read-only call returned the "
                        f"same result {previous[1]} times. Use the existing result "
                        "or try a different query."
                    ),
                    tool_name=tool_name,
                    count=previous[1],
                    signature=signature,
                )
                self._halt_decision = decision
                return decision

        return ToolGuardrailDecision(tool_name=tool_name, signature=signature)

    def after_call(
        self,
        tool_name: str,
        args: Mapping[str, Any] | None,
        result: str | None,
        *,
        failed: bool | None = None,
    ) -> ToolGuardrailDecision:
        args = _coerce_args(args)
        signature = ToolCallSignature.from_call(tool_name, args)
        consecutive_warn = self._update_consecutive_no_progress(
            tool_name, signature, result,
        )
        base = self._after_call_inner(
            tool_name, args, result, signature=signature, failed=failed,
        )
        if consecutive_warn is not None and base.action == "allow":
            return consecutive_warn
        return base

    def _update_consecutive_no_progress(
        self,
        tool_name: str,
        signature: ToolCallSignature,
        result: str | None,
    ) -> ToolGuardrailDecision | None:
        """Advance the consecutive-identical chain for an executed call."""
        if not self.config.consecutive_no_progress_enabled:
            return None
        result_hash = _result_hash(result)
        if signature != self._consecutive_signature:
            self._consecutive_signature = signature
            self._consecutive_count = 1
            self._consecutive_result_hash = result_hash
            return None
        if (
            self._consecutive_count == 0
            or result_hash != self._consecutive_result_hash
        ):
            self._consecutive_count = 1
            self._consecutive_result_hash = result_hash
            return None
        self._consecutive_count += 1
        if (
            self.config.warnings_enabled
            and self._consecutive_count >= self.config.consecutive_no_progress_warn_after
        ):
            return ToolGuardrailDecision(
                action="warn",
                code="consecutive_no_progress_warning",
                message=(
                    f"{tool_name} ran {self._consecutive_count} times in a row "
                    "with identical arguments and an identical result. Repeating "
                    "it again will be blocked — change approach."
                ),
                tool_name=tool_name,
                count=self._consecutive_count,
                signature=signature,
            )
        return None

    def _after_call_inner(
        self,
        tool_name: str,
        args: Mapping[str, Any],
        result: str | None,
        *,
        signature: ToolCallSignature,
        failed: bool | None = None,
    ) -> ToolGuardrailDecision:
        did_fail = classify_tool_failure(tool_name, result) if failed is None else failed

        if did_fail:
            exact_count = self._exact_failure_counts.get(signature, 0) + 1
            self._exact_failure_counts[signature] = exact_count
            self._no_progress.pop(signature, None)

            same_count = self._same_tool_failure_counts.get(tool_name, 0) + 1
            self._same_tool_failure_counts[tool_name] = same_count

            if (
                self.config.hard_stop_enabled
                and same_count >= self.config.same_tool_failure_halt_after
            ):
                decision = ToolGuardrailDecision(
                    action="halt",
                    code="same_tool_failure_halt",
                    message=(
                        f"Stopped {tool_name}: it failed {same_count} times this turn. "
                        "Stop retrying the same failing tool path and choose a different approach."
                    ),
                    tool_name=tool_name,
                    count=same_count,
                    signature=signature,
                )
                self._halt_decision = decision
                return decision

            if self.config.warnings_enabled and exact_count >= self.config.exact_failure_warn_after:
                return ToolGuardrailDecision(
                    action="warn",
                    code="repeated_exact_failure_warning",
                    message=(
                        f"{tool_name} has failed {exact_count} times with identical arguments. "
                        "Inspect the error and change strategy instead of retrying unchanged."
                    ),
                    tool_name=tool_name,
                    count=exact_count,
                    signature=signature,
                )

            if self.config.warnings_enabled and same_count >= self.config.same_tool_failure_warn_after:
                return ToolGuardrailDecision(
                    action="warn",
                    code="same_tool_failure_warning",
                    message=(
                        f"{tool_name} has failed {same_count} times this turn. "
                        "Change approach before retrying."
                    ),
                    tool_name=tool_name,
                    count=same_count,
                    signature=signature,
                )

            return ToolGuardrailDecision(tool_name=tool_name, count=exact_count, signature=signature)

        self._exact_failure_counts.pop(signature, None)
        self._same_tool_failure_counts.pop(tool_name, None)

        if not self._is_idempotent(tool_name):
            self._no_progress.pop(signature, None)
            return ToolGuardrailDecision(tool_name=tool_name, signature=signature)

        result_hash = _result_hash(result)
        previous = self._no_progress.get(signature)
        repeat_count = 1
        if previous is not None and previous[0] == result_hash:
            repeat_count = previous[1] + 1
        self._no_progress[signature] = (result_hash, repeat_count)

        if self.config.warnings_enabled and repeat_count >= self.config.no_progress_warn_after:
            return ToolGuardrailDecision(
                action="warn",
                code="idempotent_no_progress_warning",
                message=(
                    f"{tool_name} returned the same result {repeat_count} times. "
                    "Use the result already provided or change the query."
                ),
                tool_name=tool_name,
                count=repeat_count,
                signature=signature,
            )

        return ToolGuardrailDecision(tool_name=tool_name, count=repeat_count, signature=signature)

    def seed_from_messages(self, messages: list[dict[str, Any]] | None) -> None:
        """Rebuild the consecutive no-progress chain from conversation history.

        Guardrail instances are per-wake; without seeding, a session that
        halted on an identical-call loop would restart the count from zero
        on the next wake and re-execute the same stuck call several more
        times, growing the very pattern providers reject. Walks the message
        tail backwards over single-tool-call assistant rounds: rounds whose
        result matches the chain's result count toward the block threshold,
        guardrail-synthetic results (blocked attempts) continue the chain,
        and anything else ends it.
        """
        if not self.config.consecutive_no_progress_enabled:
            return
        results_by_call_id: dict[str, str] = {}
        chain_signature: ToolCallSignature | None = None
        chain_result_hash: str | None = None
        chain_count = 0
        for message in reversed(messages or []):
            role = message.get("role")
            if role == "tool":
                call_id = message.get("tool_call_id")
                if call_id:
                    results_by_call_id[call_id] = message.get("content") or ""
                continue
            if role != "assistant":
                break
            tool_calls = message.get("tool_calls") or []
            if len(tool_calls) != 1:
                break
            function = tool_calls[0].get("function") or {}
            raw_args = function.get("arguments", "")
            parsed_args = _safe_json_loads(raw_args) if isinstance(raw_args, str) else raw_args
            if not isinstance(parsed_args, Mapping):
                parsed_args = {}
            signature = ToolCallSignature.from_call(function.get("name", ""), parsed_args)
            if chain_signature is None:
                chain_signature = signature
            elif signature != chain_signature:
                break
            content = results_by_call_id.get(tool_calls[0].get("id", ""), "")
            parsed_result = _safe_json_loads(content)
            if isinstance(parsed_result, dict) and "guardrail" in parsed_result:
                # A blocked attempt: never executed, but still an identical
                # round in the visible history — keep the chain alive.
                chain_count += 1
                continue
            result_hash = _result_hash(content)
            if chain_result_hash is None:
                chain_result_hash = result_hash
            elif result_hash != chain_result_hash:
                break
            chain_count += 1
        if chain_signature is not None and chain_count > 0:
            self._consecutive_signature = chain_signature
            self._consecutive_result_hash = chain_result_hash
            self._consecutive_count = chain_count

    def _is_idempotent(self, tool_name: str) -> bool:
        if tool_name in self.config.mutating_tools:
            return False
        return tool_name in self.config.idempotent_tools


def toolguard_synthetic_result(
    decision: ToolGuardrailDecision,
    *,
    tool_name: str,
) -> str:
    return json.dumps(
        {
            "error": decision.message,
            "tool": tool_name,
            "guardrail": decision.to_metadata(),
        },
        ensure_ascii=False,
    )


def append_toolguard_guidance(result: str, decision: ToolGuardrailDecision) -> str:
    if decision.action not in {"warn", "halt"} or not decision.message:
        return result
    label = "Tool loop hard stop" if decision.action == "halt" else "Tool loop warning"
    return (
        f"{result or ''}\n\n[{label}: {decision.code}; "
        f"count={decision.count}; {decision.message}]"
    )


def _coerce_args(args: Mapping[str, Any] | None) -> Mapping[str, Any]:
    return args if isinstance(args, Mapping) else {}


def _result_hash(result: str | None) -> str:
    parsed = _safe_json_loads(result or "")
    canonical = (
        json.dumps(parsed, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        if parsed is not None
        else result or ""
    )
    return _sha256(canonical)


def _safe_json_loads(value: str) -> Any | None:
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return None


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on", "enabled"}:
            return True
        if lowered in {"0", "false", "no", "off", "disabled"}:
            return False
    return default


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 1 else default


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
