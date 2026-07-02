import pytest

from surogates.api.routes.git_credentials import _validate_pat


def test_validate_pat_accepts_fine_grained():
    assert _validate_pat("github_pat_ABC123") == "github_pat_ABC123"


def test_validate_pat_strips_whitespace():
    assert _validate_pat("  github_pat_ABC123  ") == "github_pat_ABC123"


def test_validate_pat_rejects_classic_or_blank():
    with pytest.raises(ValueError):
        _validate_pat("ghp_classic")
    with pytest.raises(ValueError):
        _validate_pat("   ")
