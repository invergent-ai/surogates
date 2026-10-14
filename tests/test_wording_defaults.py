"""One list of every wording key the runtime reads, and nothing left out."""

import ast
from pathlib import Path

import surogates
from surogates.followup.wording import all_default_texts

ROOT = Path(surogates.__file__).parent


def _wording_tables():
    """Every module-level ``*_TEXTS`` dict literal in the package."""
    for path in ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            target = getattr(node, "target", None) or (node.targets[0] if isinstance(node, ast.Assign) else None)
            if isinstance(target, ast.Name) and target.id.endswith("_TEXTS") and isinstance(node.value, ast.Dict):
                yield path, target.id, [k.value for k in node.value.keys if isinstance(k, ast.Constant)]


def test_every_wording_table_is_listed():
    listed = all_default_texts()
    tables = list(_wording_tables())
    assert len(tables) >= 5
    for path, name, keys in tables:
        missing = [k for k in keys if k not in listed]
        assert not missing, f"{path.relative_to(ROOT)}:{name} not in all_default_texts: {missing}"


def test_single_sentences_are_listed():
    listed = all_default_texts()
    for key in ("welcome_text", "missed_alert_text", "why_undelivered", "link_question",
                "escalation_taken", "notice_enrolled_by", "plan_limit_notice"):
        assert listed[key]
