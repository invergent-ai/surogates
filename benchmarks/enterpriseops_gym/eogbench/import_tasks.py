"""Import a pinned public release into a private, auditable task directory."""
from __future__ import annotations

import hashlib
import json
import pathlib
import re

from eogbench.dataset import parse_record, structured

HF_DATASET = "ServiceNow-AI/EnterpriseOps-Gym"
MODES = ("oracle", "plus_5_tools", "plus_10_tools", "plus_15_tools")
DOMAINS = ("calendar", "csm", "drive", "email", "hr", "hybrid", "itsm", "teams")


def import_rows(rows, output: pathlib.Path, *, revision: str, mode: str) -> dict:
    if not re.fullmatch(r"[a-f0-9]{40}", revision) or mode not in MODES:
        raise ValueError("Import needs an immutable dataset commit and a known tool mode")
    if output.exists():
        raise ValueError("Output already exists; imported datasets are immutable")
    accepted, rejected, files, seen = [], [], {}, set()
    # Validate every row before publishing any files. Unsupported rows are
    # inventoried with reasons; malformed IDs/duplicates are hard errors.
    for raw in rows:
        domain, original_id = str(raw["domain"]), str(raw["task_id"])
        stem = original_id.removeprefix("task_")
        if domain not in DOMAINS or not re.fullmatch(r"[A-Za-z0-9_-]+", stem):
            raise ValueError("Invalid public task identifier")
        key = f"{domain}/{stem}"
        if key in seen:
            raise ValueError(f"Duplicate public task: {key}")
        seen.add(key)
        record = dict(raw)
        try:
            for field in ("gym_servers_config", "verifiers", "selected_tools", "restricted_tools"):
                record[field] = structured(record.get(field), list)
            task = parse_record(record, domain, stem)
        except (ValueError, TypeError, KeyError) as exc:
            rejected.append({"task_id": key, "reason": str(exc)})
            continue
        encoded = json.dumps(record, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n"
        name = f"{domain}/task_{stem}.json"
        files[name] = encoded
        accepted.append({"benchmark": "enterpriseops_gym", "task_id": task.task_id,
                         "family": domain, "upstream_split": mode,
                         "group": f"enterpriseops_gym:{key}",
                         "file": name, "sha256": hashlib.sha256(encoded.encode()).hexdigest()})
    if not accepted:
        raise ValueError("Release has no supported tasks")
    output.mkdir(parents=True, exist_ok=False)
    for name, encoded in files.items():
        path = output / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(encoded, encoding="utf-8")
    manifest = {"dataset": HF_DATASET, "revision": revision, "mode": mode,
                "accepted": accepted, "rejected": rejected, "rows": len(seen)}
    (output / "import.json").write_text(json.dumps(manifest, indent=2) + "\n")
    # Identifiers/descriptors only: this is safe input to harness-evolve split.
    catalog = [{k: v for k, v in task.items() if k not in ("file", "sha256")} for task in accepted]
    (output / "catalog.json").write_text(json.dumps(catalog, indent=2) + "\n")
    return manifest


def download(revision: str, mode: str, output: pathlib.Path) -> dict:
    if not re.fullmatch(r"[a-f0-9]{40}", revision) or mode not in MODES:
        raise ValueError("Import needs an immutable dataset commit and a known tool mode")
    from huggingface_hub import snapshot_download
    import pyarrow.parquet as pq

    root = pathlib.Path(snapshot_download(repo_id=HF_DATASET, repo_type="dataset", revision=revision,
                                         allow_patterns=[f"{mode}/*.parquet"]))
    paths = sorted((root / mode).glob("*.parquet"))
    if not paths:
        raise ValueError("No parquet shards in the selected release/mode")
    rows = (row for path in paths for row in pq.read_table(path).to_pylist())
    return import_rows(rows, output, revision=revision, mode=mode)
