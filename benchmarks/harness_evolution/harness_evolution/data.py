"""Freeze external task/verifier inputs outside the candidate's filesystem."""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path, PurePosixPath

from harness_evolution.benchmarks import upstream_split
from harness_evolution.config import read_json, write_json


def tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    if not root.is_dir() or root.is_symlink():
        raise ValueError("Private data directory is missing or linked")
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not (path.is_dir() or path.is_file()):
            raise ValueError("Private data contains a link or special file")
        if path.is_file():
            file_hash = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    file_hash.update(chunk)
            digest.update(json.dumps([path.relative_to(root).as_posix(), file_hash.hexdigest()]).encode())
    return digest.hexdigest()


def copy_file(source_root: Path, relative: str, target_root: Path) -> Path:
    path = PurePosixPath(relative)
    if path.is_absolute() or ".." in path.parts or "\\" in relative or str(path) != relative:
        raise ValueError("Unsafe private data path")
    source = source_root / relative
    if source.is_symlink() or not source.is_file() or not source.resolve().is_relative_to(source_root.resolve()):
        raise ValueError(f"Missing or unsafe input file: {relative}")
    target = target_root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    return target


def freeze_data(config: dict, root: Path) -> dict:
    root.mkdir(exist_ok=False)
    bindings = {}
    data = config.get("benchmark_data", {})
    tasks = config["task_manifest"]
    eog = [t for t in tasks if t["benchmark"] == "enterpriseops_gym"]
    if eog:
        options = data.get("enterpriseops_gym", {})
        source = Path(options["tasks_dir"])
        seeds = Path(options["seed_root"])
        manifest = read_json(source / "import.json")
        if manifest["revision"] != config["dataset_revisions"]["enterpriseops_gym"]:
            raise ValueError("EnterpriseOps import does not match the pinned dataset revision")
        indexed = {t["task_id"]: t for t in manifest["accepted"]}
        task_root, seed_root = root / "enterpriseops_gym/tasks", root / "enterpriseops_gym/seeds"
        for task in eog:
            if task["task_id"] not in indexed or upstream_split(task) != manifest["mode"]:
                raise ValueError("EnterpriseOps task/mode is absent from the imported release")
            entry = indexed[task["task_id"]]
            path = copy_file(source, entry["file"], task_root)
            if hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
                raise ValueError("Imported EnterpriseOps task was modified")
            raw = read_json(path)
            gyms = raw["gym_servers_config"]
            if not isinstance(gyms, list) or len(gyms) != 1:
                raise ValueError("Only imported single-gym tasks can be evaluated")
            copy_file(seeds, gyms[0]["seed_database_file"], seed_root)
        write_json(task_root / "import.json", manifest)
        bindings["enterpriseops_gym"] = {"tasks_dir": str(task_root), "seed_root": str(seed_root), "mode": manifest["mode"]}
    dab = [t for t in tasks if t["benchmark"] == "dabstep"]
    if dab:
        source = Path(data["dabstep"]["answer_key"])
        target = copy_file(source.parent, source.name, root / "dabstep")
        key = read_json(target)
        for task in dab:
            entry = key.get("entries", {}).get(task["task_id"], {})
            if not isinstance(entry.get("answer"), str) or not entry["answer"].strip() or not entry.get("source"):
                raise ValueError("DABstep reference key does not cover every requested task")
        bindings["dabstep"] = {"answer_key": str(target)}
    write_json(root / "bindings.json", bindings)
    return bindings
