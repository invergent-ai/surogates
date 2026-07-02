"""Tests for the PR-workflow prompt augmenter + PR-URL parser."""

from __future__ import annotations

from surogates.coding_agents.pr_workflow import augment_prompt, parse_pr_url


def test_augment_mentions_branch_commit_push_pr():
    p = augment_prompt("fix the login bug", branch="fix/login-1", default_branch="main")
    assert "fix/login-1" in p
    assert "--base main" in p
    for kw in ("commit", "push", "pull request", "PR URL"):
        assert kw.lower() in p.lower()
    # The original ask survives verbatim.
    assert "fix the login bug" in p


def test_parse_pr_url_extracts_first_pr_link():
    out = "Done.\nhttps://github.com/acme/api/pull/42 opened.\n"
    assert parse_pr_url(out) == "https://github.com/acme/api/pull/42"


def test_parse_pr_url_takes_the_first_of_many():
    out = "https://github.com/acme/api/pull/7 and https://github.com/acme/api/pull/9"
    assert parse_pr_url(out) == "https://github.com/acme/api/pull/7"


def test_parse_pr_url_none_when_absent():
    assert parse_pr_url("no pr here") is None


def test_parse_pr_url_none_on_empty():
    assert parse_pr_url("") is None
    assert parse_pr_url(None) is None
