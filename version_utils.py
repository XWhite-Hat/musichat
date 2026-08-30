"""
version_utils.py — one correct version parser, shared by every caller.

Previously updater.py and pyside_downloader.py each carried their own copy of
this logic, and both had the same defect: they split on "." and coerced any
segment that failed int() to 0.  That throws away the *number* as well as the
suffix, so "1.0.2-beta.1" parsed as (1, 0, 0) rather than (1, 0, 2).  Since the
release workflow explicitly accepts v1.2.3-beta.1 tags, a user on 1.0.9-beta.1
could be told that the older 1.0.5 was an upgrade.
"""
from __future__ import annotations

import re

__all__ = ["is_newer", "parse_version"]

# Leading "v", then dot-separated numbers, then an optional pre-release suffix
# introduced by "-" (semver) or directly appended (e.g. "6.11.1rc1").
_VERSION_RE = re.compile(
    r"^\s*v?(?P<release>\d+(?:\.\d+)*)(?P<pre>[-.+]?[A-Za-z][\w.+-]*)?\s*$"
)


def parse_version(v: str, parts: int = 3) -> tuple:
    """
    Parse *v* into a tuple that sorts correctly against other parsed versions.

    The result is the numeric release segments padded to *parts*, followed by a
    final element that orders pre-releases below their own final release:
    0 for a pre-release, 1 for a final release.

        parse_version("1.0.2")        -> (1, 0, 2, 1)
        parse_version("1.0.2-beta.1") -> (1, 0, 2, 0)
        parse_version("1.0.10")       -> (1, 0, 10, 1)

    So 1.0.2-beta.1 < 1.0.2, while both are above 1.0.1 — which is the property
    the old implementation lacked.  Unparseable input sorts lowest.
    """
    m = _VERSION_RE.match(v or "")
    if not m:
        return (0,) * parts + (0,)

    nums = [int(n) for n in m.group("release").split(".")][:parts]
    nums += [0] * (parts - len(nums))
    is_final = 0 if m.group("pre") else 1
    return (*tuple(nums), is_final)


def is_newer(remote: str, local: str) -> bool:
    """True when *remote* is a strictly newer version than *local*."""
    if not remote or local == "dev":
        return False
    return parse_version(remote) > parse_version(local)
