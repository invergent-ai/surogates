# Test recipes. pytest's defaults live in pyproject.toml
# ([tool.pytest.ini_options]): tests/, 16 workers, and the opt-in markers
# (browser_e2e, browser_e2e_k8s, live, desktop) and slow tests skipped.
# Extra arguments go straight to pytest, e.g.
#   just test tests/test_saga.py -n0

set positional-arguments

pytest := ".venv/bin/python -m pytest"
opt_in := "not browser_e2e and not browser_e2e_k8s and not live and not desktop"

# The default suite: everything except the opt-in and slow tests.
test *args:
    {{pytest}} "$@"

# Only the slow tests. Run before every release.
test-slow *args:
    {{pytest}} -m "slow and {{opt_in}}" "$@"
