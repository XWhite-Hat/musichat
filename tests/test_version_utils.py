"""
Version comparison.

Regression cover for the parser that coerced any non-numeric segment to 0, so
"1.0.2-beta.1" became (1, 0, 2) -> (1, 0, 0) and the updater could offer a
downgrade to anyone running a pre-release build.
"""
from __future__ import annotations

import pytest

from version_utils import is_newer, parse_version


@pytest.mark.parametrize("version, expected", [
    ("1.0.2",        (1, 0, 2, 1)),
    ("v1.0.2",       (1, 0, 2, 1)),      # leading v, as GitHub tags carry
    ("1.0.10",       (1, 0, 10, 1)),     # not string-compared against 1.0.9
    ("1.0.2-beta.1", (1, 0, 2, 0)),      # patch preserved; marked pre-release
    ("6.11.1rc1",    (6, 11, 1, 0)),     # suffix with no separator
    ("2026.8.19",    (2026, 8, 19, 1)),  # yt-dlp style calendar version
    ("1.2",          (1, 2, 0, 1)),      # short versions pad
    ("garbage",      (0, 0, 0, 0)),      # unparseable sorts lowest
    ("",             (0, 0, 0, 0)),
])
def test_parse_version(version, expected):
    assert parse_version(version) == expected


def test_prerelease_sorts_below_its_release():
    assert parse_version("1.0.2-beta.1") < parse_version("1.0.2")


def test_prerelease_still_sorts_above_earlier_release():
    """The old parser got this wrong: 1.0.2-beta.1 collapsed to 1.0.0."""
    assert parse_version("1.0.2-beta.1") > parse_version("1.0.1")


@pytest.mark.parametrize("remote, local, expected", [
    ("1.0.2", "1.0.1", True),
    ("1.0.1", "1.0.2", False),
    ("1.0.2", "1.0.2", False),
    ("1.0.10", "1.0.9", True),
    ("1.0.2", "1.0.2-beta.1", True),      # final beats its own pre-release
    ("1.0.2-beta.1", "1.0.2", False),
    ("1.0.9-beta.1", "1.0.8", True),
])
def test_is_newer(remote, local, expected):
    assert is_newer(remote, local) is expected


def test_never_offers_a_downgrade_to_prerelease_users():
    """The concrete user-facing symptom of the old bug."""
    assert is_newer("1.0.5", "1.0.9-beta.1") is False


def test_dev_build_never_prompts():
    assert is_newer("99.0.0", "dev") is False


def test_empty_remote_never_prompts():
    assert is_newer("", "1.0.0") is False


def test_updater_and_downloader_share_this_implementation():
    """Both modules previously had their own subtly-broken copy."""
    import pyside_downloader
    import updater
    assert updater._parse_ver("1.0.2-beta.1")[:3] == (1, 0, 2)
    assert pyside_downloader._version_tuple("6.11.1")[:3] == (6, 11, 1)
