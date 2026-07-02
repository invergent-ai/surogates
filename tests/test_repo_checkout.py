from surogates.coding_agents.repo_checkout import (
    clone_script,
    clone_url,
    fix_branch_name,
    git_auth_env,
    repo_dir_name,
)


def test_branch_name_is_slugged_and_timestamped():
    b = fix_branch_name("Fix the Login Bug!", now=1_700_000_000.0)
    assert b.startswith("fix/fix-the-login-bug-")
    assert b.split("-")[-1].isdigit()


def test_repo_dir_name_strips_git_suffix():
    assert repo_dir_name("https://github.com/acme/api.git") == "api"
    assert repo_dir_name("https://github.com/acme/api") == "api"


def test_clone_url_converts_github_ssh_to_https():
    assert clone_url("git@github.com:acme/api.git") == "https://github.com/acme/api.git"
    assert clone_url("ssh://git@github.com/acme/api") == "https://github.com/acme/api"


def test_clone_script_never_inlines_the_token():
    s = clone_script(
        repo_url="https://github.com/acme/api",
        default_branch="main",
        dest="/workspace/api",
        branch="fix/x-1",
    )
    assert "$GH_TOKEN" in s
    assert "github_pat_" not in s
    assert "--depth 1" in s and "main" in s and "fix/x-1" in s
    assert "credential.helper" in s
    assert "git config --global" not in s


def test_clone_script_quotes_shell_values():
    s = clone_script(
        repo_url="https://github.com/acme/api",
        default_branch="main; echo nope",
        dest="/workspace/api dir",
        branch="fix/x-1",
    )
    assert "--branch main; echo" not in s
    assert "--branch 'main; echo nope'" in s
    assert "'/workspace/api dir'" in s


def test_git_auth_env_contains_only_runtime_secret():
    assert git_auth_env("github_pat_x") == {
        "GH_TOKEN": "github_pat_x",
        "GIT_TERMINAL_PROMPT": "0",
        "GH_PROMPT_DISABLED": "1",
    }
