"""Remote URL parsing: SSH, ssh://, HTTPS, with/without .git."""

import pytest

from quality_gate_mcp.git_ops import parse_remote


@pytest.mark.parametrize(
    "url,expected",
    [
        ("git@github.com:owner/repo.git", "owner/repo"),
        ("git@github.com:owner/repo", "owner/repo"),
        ("https://github.com/owner/repo.git", "owner/repo"),
        ("https://github.com/owner/repo", "owner/repo"),
        ("https://github.com/owner/repo/", "owner/repo"),
        ("http://github.com/owner/repo.git", "owner/repo"),
        ("ssh://git@github.com/owner/repo.git", "owner/repo"),
        ("ssh://git@github.com:2222/owner/repo.git", "owner/repo"),
        ("git@gitlab.com:group/project.git", "group/project"),
        ("  git@github.com:owner/repo.git  ", "owner/repo"),
        ("https://github.com/org/sub/repo.git", "sub/repo"),
    ],
)
def test_parse_remote_valid(url, expected):
    assert parse_remote(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "",
        "not-a-url",
        "https://github.com/owner",
        "git@github.com:",
        "https://github.com/",
    ],
)
def test_parse_remote_invalid(url):
    with pytest.raises(ValueError):
        parse_remote(url)
