"""Prompt profiles -- model-specific prompt and tool-description variants.

The platform prompt (``harness/prompts/``) and the builtin tool
descriptions are written for every model the platform serves. A
*profile* replaces parts of them for one model family whose behaviour
was measured against them, without changing what any other model sees:

- a fragment at ``prompts/profiles/<profile>/<key>.md`` is used instead
  of ``prompts/<key>.md`` (see :class:`~surogates.harness.prompt_library.PromptLibrary`);
  an empty overlay body drops the fragment;
- ``prompts/profiles/<profile>/tools.yaml`` replaces tool and parameter
  descriptions (see :func:`apply_tool_profile`);
- ``prompts/profiles/<profile>/skills.yaml`` replaces or hides skills-index
  lines (see :func:`skill_index_entry`); ``SKILL.md`` files are untouched;
- ``prompts/profiles/<profile>/profile.yaml`` holds switches read by the
  prompt builder (``compact_context``).

New fragment keys a profile may add (no base fragment): ``guidance/skills_index``
(the skills section around ``{index}``) and ``guidance/kb_actions`` (appended
to the authoritative-knowledge-base paragraph).

``benchmarks/prompt_lab`` holds the A/B behind the ``surogate`` profile.

Resolution order (first hit wins):

1. ``session.config["prompt_profile"]``
2. the org config's ``prompt_profile``
3. ``SUROGATES_PROMPT_PROFILES`` (deployment-wide), e.g. ``surogate=surogate``
   when the gateway routes the ``surogate`` tier sentinel to Surogate 3.x
4. the model id itself, when it names a model with a profile

Levels 1-3 take either a profile name (every model) or a mapping of model
id to profile name (``"*"`` for the rest); levels 1-2 accept a dict, level 3
the ``model=profile,model=profile`` form. ``"default"`` (or ``"none"``)
turns profiles off. A per-model form is what keeps a deployment from handing
the profile to the ``surogate-pro`` sentinel, which Claude serves.
"""

from __future__ import annotations

import copy
import functools
import logging
import os
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

SUROGATE_PROFILE = "surogate"

PROFILES_ROOT: Path = Path(__file__).parent / "prompts" / "profiles"

#: Model-id substrings that select a profile on their own. The ``surogate``
#: and ``surogate-pro`` tier sentinels are deliberately absent: what serves
#: a sentinel is the gateway's choice (Pro is Claude), so a deployment that
#: routes the base tier to Surogate 3.x opts in with
#: SUROGATES_PROMPT_PROFILES=surogate=surogate.
_MODEL_PROFILES: tuple[tuple[str, str], ...] = (
    ("surogate3", SUROGATE_PROFILE),
    ("surogate-3", SUROGATE_PROFILE),
    ("surogate/surogate", SUROGATE_PROFILE),
)

_OFF = frozenset({"default", "none", "off", ""})


def resolve_prompt_profile(
    model_id: str | None,
    *,
    org_config: dict | None = None,
    session_config: dict | None = None,
) -> str | None:
    """Return the active profile name, or ``None`` for the default prompt."""
    lower = (model_id or "").lower()
    for source in (session_config or {}, org_config or {}):
        hit = _pick(source.get("prompt_profile"), lower)
        if hit is not None:
            return _known(hit)
    env = os.environ.get("SUROGATES_PROMPT_PROFILES")
    if env:
        mapping = dict(
            (k.strip().lower(), v.strip())
            for k, _, v in (item.partition("=") for item in env.split(","))
            if k.strip() and v.strip()
        )
        hit = _pick(mapping, lower)
        if hit is not None:
            return _known(hit)
    for token, profile in _MODEL_PROFILES:
        if token in lower:
            return profile
    return None


def _pick(value: Any, model_lower: str) -> str | None:
    """A configured value for *model_lower*: a name applies to every model."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        lowered = {str(k).lower(): v for k, v in value.items()}
        hit = lowered.get(model_lower, lowered.get("*"))
        return hit if isinstance(hit, str) else None
    return None


def _known(name: str) -> str | None:
    name = name.strip().lower()
    if name in _OFF:
        return None
    if not (PROFILES_ROOT / name).is_dir():
        logger.warning("unknown prompt profile %r; using the default prompt", name)
        return None
    return name


@functools.lru_cache(maxsize=8)
def _tool_overrides(profile: str) -> dict[str, Any]:
    path = PROFILES_ROOT / profile / "tools.yaml"
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a mapping of tool name to overrides")
    return data


def apply_tool_profile(schemas: list[dict[str, Any]], profile: str | None) -> list[dict[str, Any]]:
    """Return *schemas* with the profile's tool/parameter descriptions.

    ``tools.yaml`` maps a tool name to ``description`` and/or
    ``parameters: {param: description}``. Only descriptions change: names,
    types, enums and required lists stay the registry's, so dispatch and
    validation are unaffected. Untouched schemas are returned by reference;
    the input is never mutated.
    """
    if not profile:
        return schemas
    overrides = _tool_overrides(profile)
    if not overrides:
        return schemas
    out: list[dict[str, Any]] = []
    for schema in schemas:
        fn = schema.get("function") or {}
        o = overrides.get(fn.get("name"))
        if not o:
            out.append(schema)
            continue
        clone = copy.deepcopy(schema)
        cfn = clone["function"]
        if isinstance(o.get("description"), str):
            cfn["description"] = o["description"].strip()
        props = (cfn.get("parameters") or {}).get("properties") or {}
        for pname, pdesc in (o.get("parameters") or {}).items():
            if pname in props and isinstance(pdesc, str):
                props[pname]["description"] = pdesc.strip()
            elif pname in props and pdesc is None:
                props[pname].pop("description", None)
        out.append(clone)
    return out


@functools.lru_cache(maxsize=8)
def _settings(profile: str) -> dict[str, Any]:
    path = PROFILES_ROOT / profile / "profile.yaml"
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a mapping")
    return data


def profile_settings(profile: str | None) -> dict[str, Any]:
    """The profile's ``profile.yaml`` switches (empty without a profile)."""
    return dict(_settings(profile)) if profile else {}


@functools.lru_cache(maxsize=8)
def _skill_overrides(profile: str) -> dict[str, Any]:
    path = PROFILES_ROOT / profile / "skills.yaml"
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a mapping of skill name to overrides")
    return data


def skill_index_entry(profile: str | None, name: str, description: str) -> str | None:
    """The description a profile shows for skill *name* in the skills index.

    ``skills.yaml`` maps a skill name to ``description`` (a shorter or
    re-aimed index line; the skill's SKILL.md is unchanged) and/or
    ``hidden: true`` (left out of the index; still loadable by name and as
    a slash command). Returns ``None`` when the skill is hidden.
    """
    if not profile:
        return description
    o = _skill_overrides(profile).get(name)
    if not isinstance(o, dict):
        return description
    if o.get("hidden"):
        return None
    d = o.get("description")
    return d.strip() if isinstance(d, str) else description
