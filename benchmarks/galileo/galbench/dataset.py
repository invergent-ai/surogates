"""Load Agent Leaderboard v2 scenarios and partition them into dev/holdout.

Everything comes from the ungated HuggingFace dataset
``galileo-ai/agent-leaderboard-v2``: per domain (banking, healthcare,
insurance, investment, telecom) 100 scenarios (``adaptive_tool_use``),
100 personas, and a 20-tool catalog with response schemas. 500 scenarios
total, cached by ``huggingface_hub``.

The frozen split takes 20 scenarios per domain into dev (100) and
leaves 400 in holdout, seed-fixed; ``tests/test_dataset.py`` re-derives
it from a committed fixture so silent drift fails the suite.
"""
from __future__ import annotations

import json
import pathlib
import random
from dataclasses import dataclass
from typing import Any

HF_DATASET = "galileo-ai/agent-leaderboard-v2"
DOMAINS = ("banking", "healthcare", "insurance", "investment", "telecom")
DEFAULT_SEED = 20260906
SPLITS_PATH = pathlib.Path(__file__).parent / "splits" / "v2.json"


@dataclass(frozen=True)
class Scenario:
    scenario_id: str  # "<domain>-<index>", e.g. "banking-007"
    domain: str
    persona: dict[str, Any]
    first_message: str
    user_goals: tuple[str, ...]
    tools: tuple[dict[str, Any], ...]  # full schemas incl. response_schema


def download_dataset() -> str:
    import huggingface_hub

    return huggingface_hub.snapshot_download(
        repo_id=HF_DATASET,
        repo_type="dataset",
        allow_patterns=[
            "adaptive_tool_use/*", "personas/*", "tools/*",
        ],
    )


def _read_parquet(root: str, config: str, domain: str) -> list[dict]:
    import pandas as pd

    path = pathlib.Path(root) / config / f"{domain}-00000-of-00001.parquet"
    if not path.exists():
        raise SystemExit(f"dataset file missing from snapshot: {path.name}")
    frame = pd.read_parquet(path)
    return frame.to_dict(orient="records")


def _listish(value) -> list:
    """Parquet columns round-trip as lists, numpy arrays, JSON strings or
    None depending on writer version; normalize to a plain list. Never
    use ``or``-defaulting on these values -- numpy arrays raise on
    boolean coercion."""
    if value is None:
        return []
    if isinstance(value, str):
        return json.loads(value) if value.strip() else []
    if hasattr(value, "tolist"):
        return list(value.tolist())
    return list(value)


def _dictish(value) -> dict:
    if value is None:
        return {}
    if isinstance(value, str):
        return json.loads(value) if value.strip() else {}
    return dict(value)


def load_domain(domain: str, snapshot_dir: str | None = None) -> list[Scenario]:
    root = snapshot_dir or download_dataset()
    personas = _read_parquet(root, "personas", domain)
    tool_rows = _read_parquet(root, "tools", domain)

    tools = []
    for row in tool_rows:
        tools.append({
            "title": row.get("title"),
            "description": row.get("description"),
            "type": row.get("type", "object"),
            "properties": _dictish(row.get("properties")),
            "required": _listish(row.get("required")),
            "response_schema": _dictish(row.get("response_schema")),
        })
    tools_t = tuple(tools)

    scenarios = []
    for i, row in enumerate(_read_parquet(root, "adaptive_tool_use", domain)):
        persona_index = int(row.get("persona_index") or 0)
        persona = dict(personas[persona_index]) if persona_index < len(personas) else {}
        persona = {k: (v.tolist() if hasattr(v, "tolist") else v)
                   for k, v in persona.items()}
        scenarios.append(Scenario(
            scenario_id=f"{domain}-{i:03d}",
            domain=domain,
            persona=persona,
            first_message=str(row.get("first_message") or ""),
            user_goals=tuple(map(str, _listish(row.get("user_goals")))),
            tools=tools_t,
        ))
    return scenarios


def make_split(
    ids: list[str],
    dev_per_domain: int = 20,
    seed: int = DEFAULT_SEED,
) -> tuple[list[str], list[str]]:
    """Partition scenario ids into (dev, holdout), per-domain quota."""
    by_domain: dict[str, list[str]] = {}
    for sid in sorted(ids):
        by_domain.setdefault(sid.rsplit("-", 1)[0], []).append(sid)

    rng = random.Random(seed)
    dev: list[str] = []
    holdout: list[str] = []
    for domain in sorted(by_domain):
        pool = list(by_domain[domain])
        rng.shuffle(pool)
        dev.extend(pool[:dev_per_domain])
        holdout.extend(pool[dev_per_domain:])
    return sorted(dev), sorted(holdout)


def frozen_split() -> dict[str, list[str]]:
    with open(SPLITS_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def load_scenarios(
    split: str = "dev",
    domains: tuple[str, ...] = DOMAINS,
    snapshot_dir: str | None = None,
) -> list[Scenario]:
    """Load scenarios for ``split`` ("dev", "holdout" or "all")."""
    root = snapshot_dir or download_dataset()
    scenarios: list[Scenario] = []
    for domain in domains:
        scenarios.extend(load_domain(domain, snapshot_dir=root))
    if split == "all":
        return scenarios
    wanted = set(frozen_split()[split])
    return [s for s in scenarios if s.scenario_id in wanted]
