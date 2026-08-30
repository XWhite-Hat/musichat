"""
dataclass_utils.py — one place that resolves dataclass field types.

`from __future__ import annotations` turns every annotation into a string, so
anything reading `dataclasses.fields(...)[i].type` gets `"Optional[int]"` rather
than the type object.  config.py and server/settings_app.py each had their own
copy of the code that turned that string back into a type, and both did it with
a bare `eval()`.

The strings come from the modules' own source rather than from user input, so
that was never a security hole — but it is fragile (it re-implements what the
typing module already does correctly), and two near-identical copies is how a
fix lands in one and silently misses the other.  `typing.get_type_hints()` is
the supported API for exactly this, and it caches per class.
"""
from __future__ import annotations

import dataclasses
import functools
import sys
import types
import typing
from typing import Any

__all__ = ["field_type_for", "resolve_hints", "unwrap_optional"]


@functools.cache
def resolve_hints(cls: type) -> dict[str, Any]:
    """
    Return {field_name: resolved_type} for a dataclass.

    Falls back to the raw annotation when a hint cannot be resolved (for
    example a forward reference to a type that is not importable here), so a
    single unresolvable field never breaks loading an entire config.
    """
    try:
        return typing.get_type_hints(cls, sys.modules[cls.__module__].__dict__)
    except Exception:
        return dict(getattr(cls, "__annotations__", {}))


def field_type_for(cls: type, name: str) -> Any:
    """Resolved type of a single dataclass field, or None if unknown."""
    return resolve_hints(cls).get(name)


def unwrap_optional(field_type: Any) -> tuple[Any, bool]:
    """
    Split `Optional[X]` into `(X, True)`; anything else into `(field_type, False)`.

    Handles both typing.Optional/Union and the PEP 604 `X | None` form, which
    is a types.UnionType and is *not* matched by `origin is typing.Union`.
    """
    origin = typing.get_origin(field_type)
    if origin is typing.Union or origin is types.UnionType:
        inner = [t for t in typing.get_args(field_type) if t is not type(None)]
        if len(inner) == 1:
            return inner[0], True
        return field_type, True
    return field_type, False


def dict_to_dataclass(cls: type, d: Any) -> Any:
    """
    Build *cls* from a plain dict, recursing into nested dataclass fields.

    Unknown keys are ignored so an old config file loaded by a newer build does
    not raise on fields that have since been removed.
    """
    if not isinstance(d, dict):
        return d
    if not dataclasses.is_dataclass(cls):
        return d

    hints = resolve_hints(cls)
    known = {f.name for f in dataclasses.fields(cls)}
    kwargs: dict[str, Any] = {}

    for key, val in d.items():
        if key not in known:
            continue
        field_type = hints.get(key)
        base, _ = unwrap_optional(field_type)
        if dataclasses.is_dataclass(base) and isinstance(val, dict):
            kwargs[key] = dict_to_dataclass(base, val)
        else:
            kwargs[key] = val

    return cls(**kwargs)
