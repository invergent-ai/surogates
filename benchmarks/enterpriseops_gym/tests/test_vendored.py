"""Vendored-checkout integration: imports and real sample tasks.

Skipped when the checkout is absent (it is gitignored); with it present
these prove the exact interfaces the runner relies on.
"""
import pytest

from eogbench import vendor


def _vendor_present() -> bool:
    try:
        vendor.home()
        return True
    except SystemExit:
        return False


pytestmark = pytest.mark.skipif(
    not _vendor_present(), reason="vendor/EnterpriseOps-Gym not cloned"
)


def test_vendored_modules_import_with_expected_interfaces():
    vendor.pythonpath()
    from benchmark.mcp_client import (  # type: ignore
        MCPClient,
        create_database_from_file,
        delete_database,
    )
    from benchmark.verifier import VerifierEngine  # type: ignore

    assert callable(create_database_from_file)
    assert callable(delete_database)
    client = MCPClient(base_url="http://localhost:8001",
                       database_id="db_x")
    assert client.database_id == "db_x"
    # The exact construction the runner performs.
    from benchmark.models import VerifierConfig  # type: ignore

    engine = VerifierEngine(mcp_clients={"gym": client}, llm_client=None)
    assert engine._get_mcp_client_for_gym("gym") is client
    config = VerifierConfig(verifier_type="database_state",
                            validation_config={"query": "SELECT 1"})
    assert config.verifier_type == "database_state"


def test_sample_tasks_parse():
    from eogbench.dataset import load_tasks

    tasks = load_tasks()
    assert tasks, "vendored data/revised has no tasks"
    for t in tasks:
        assert t.gym_url.startswith("http")
        assert t.verifiers, f"{t.task_id} has no verifiers"
        assert t.system_prompt and t.user_prompt
