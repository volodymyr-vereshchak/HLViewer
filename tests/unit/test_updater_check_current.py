"""A stored check is shown only while it still describes what is installed."""
from updater.main import check_is_current

OLD, NEW = "a" * 40, "b" * 40
CONFIG = {"repos": {"backend": {"url": "https://github.com/o/b.git", "branch": "master"}}}


def _check(head=OLD, branch="master"):
    return {"repos": {"backend": {"url": "https://github.com/o/b.git", "branch": branch,
                                  "head": {"sha": head}, "ahead": 1}}}


def test_check_of_the_installed_commit_is_current():
    assert check_is_current(_check(), {"backend": {"sha": OLD}}, CONFIG)


def test_update_installed_since_makes_it_stale():
    # The bug: after a successful update the old "1 new commit" stayed up.
    assert not check_is_current(_check(), {"backend": {"sha": NEW}}, CONFIG)


def test_switched_branch_makes_it_stale():
    assert not check_is_current(_check(branch="updater-test"), {"backend": {"sha": OLD}}, CONFIG)
