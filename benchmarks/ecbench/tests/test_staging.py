"""Upload-plan construction from a fake vendored checkout."""
import pytest

from ecbench.staging import StagingError, stage_plan


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    root = tmp_path / "E-CommerceBench"
    (root / "tools" / "opponent").mkdir(parents=True)
    (root / "tools" / "__pycache__").mkdir()
    (root / "data").mkdir()
    (root / "agent").mkdir()
    (root / "tools" / "ecommerce_env.py").write_text("ENV")
    (root / "tools" / "opponent" / "kernel_manager.py").write_text("K")
    (root / "tools" / "__pycache__" / "junk.cpython-312.pyc").write_text("x")
    (root / "tools" / "stale.pyc").write_text("x")
    (root / "data" / "products.csv").write_text("sku,name\n")
    (root / "agent" / "ecommerce_tool_manager.py").write_text("TM")
    monkeypatch.setenv("ECBENCH_HOME", str(root))
    return root


def test_stage_plan_layout(fake_home):
    plan = stage_plan()
    paths = {s.workspace_path for s in plan}
    assert "sim/tools/ecommerce_env.py" in paths
    assert "sim/tools/opponent/kernel_manager.py" in paths
    assert "sim/data/products.csv" in paths
    # The tool manager uploads flat into sim/, outside upstream's agent
    # package, so importing it never pulls their LLM stack.
    assert "sim/ecommerce_tool_manager.py" in paths
    assert "ecsim.py" in paths
    assert not any("__pycache__" in p or p.endswith(".pyc") for p in paths)


def test_stage_plan_requires_tool_manager(fake_home):
    (fake_home / "agent" / "ecommerce_tool_manager.py").unlink()
    with pytest.raises(StagingError, match="ecommerce_tool_manager"):
        stage_plan()


def test_missing_checkout_exits(tmp_path, monkeypatch):
    monkeypatch.setenv("ECBENCH_HOME", str(tmp_path / "nope"))
    with pytest.raises(SystemExit, match="checkout not found"):
        stage_plan()
