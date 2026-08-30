"""
Dataclass field-type resolution.

`from __future__ import annotations` stringifies every annotation, so config
loading has to turn those strings back into types.  config.py and
settings_app.py each carried their own eval()-based copy of that logic; these
tests pin the behaviour of the single shared implementation that replaced them,
including the PEP 604 `X | None` form the old code did not handle.
"""
from __future__ import annotations

import types
import typing
from dataclasses import dataclass, field

from dataclass_utils import dict_to_dataclass, field_type_for, unwrap_optional


@dataclass
class Inner:
    depth: int = 1
    label: str = "inner"


@dataclass
class Outer:
    name: str = "outer"
    count: int = 0
    ratio: float = 0.0
    flag: bool = False
    maybe: int | None = None
    legacy: typing.Optional[str] = None   # noqa: UP007 - the old form on purpose
    inner: Inner = field(default_factory=Inner)
    items: list = field(default_factory=list)


def test_resolves_stringified_annotations():
    assert field_type_for(Outer, "count") is int
    assert field_type_for(Outer, "name") is str


def test_resolves_nested_dataclass_type():
    assert field_type_for(Outer, "inner") is Inner


def test_unknown_field_returns_none():
    assert field_type_for(Outer, "nope") is None


def test_unwrap_pep604_optional():
    """`int | None` is types.UnionType, which the old eval-based code missed."""
    unwrapped, optional = unwrap_optional(field_type_for(Outer, "maybe"))
    assert optional is True
    assert unwrapped is int


def test_unwrap_typing_optional():
    unwrapped, optional = unwrap_optional(field_type_for(Outer, "legacy"))
    assert optional is True
    assert unwrapped is str


def test_unwrap_leaves_plain_types_alone():
    unwrapped, optional = unwrap_optional(int)
    assert optional is False
    assert unwrapped is int


def test_pep604_union_is_recognised_as_a_union():
    assert typing.get_origin(int | None) is types.UnionType


def test_dict_to_dataclass_populates_scalars():
    got = dict_to_dataclass(Outer, {"name": "x", "count": 5, "ratio": 1.5, "flag": True})
    assert (got.name, got.count, got.ratio, got.flag) == ("x", 5, 1.5, True)


def test_dict_to_dataclass_recurses_into_nested_dataclass():
    got = dict_to_dataclass(Outer, {"inner": {"depth": 9, "label": "deep"}})
    assert isinstance(got.inner, Inner)
    assert got.inner.depth == 9


def test_dict_to_dataclass_ignores_unknown_keys():
    """An old config file must not break a newer build that dropped a field."""
    got = dict_to_dataclass(Outer, {"name": "x", "removed_in_v2": True})
    assert got.name == "x"


def test_dict_to_dataclass_keeps_defaults_for_absent_keys():
    got = dict_to_dataclass(Outer, {})
    assert got.name == "outer"
    assert isinstance(got.inner, Inner)


def test_dict_to_dataclass_passes_through_non_dicts():
    assert dict_to_dataclass(Outer, "not a dict") == "not a dict"


def test_real_config_round_trips_nested_and_optional_fields(data_dir):
    """The behaviour that actually matters: a full AppConfig survives a save."""
    import config

    cfg = config.load_config()
    cfg.twitch.tier_vip.queue_limit = 42
    cfg.audio.output_device = None
    cfg.spectrogram_presets[0].bar_count = 128
    config.save_config(cfg)

    reloaded = config.load_config()
    assert reloaded.twitch.tier_vip.queue_limit == 42
    assert type(reloaded.twitch.tier_vip).__name__ == "TierConfig"
    assert reloaded.audio.output_device is None
    assert reloaded.spectrogram_presets[0].bar_count == 128
    assert type(reloaded.spectrogram_presets[0]).__name__ == "SpectrogramConfig"


def test_settings_api_coerces_values_to_field_types(data_dir):
    """_set_typed is the settings page's write path — it must still coerce."""
    import config
    from server.settings_app import _set_typed

    cfg = config.load_config()

    _set_typed(cfg.twitch.tier_vip, "queue_limit", "7")
    assert cfg.twitch.tier_vip.queue_limit == 7

    _set_typed(cfg.twitch.tier_vip, "can_request", False)
    assert cfg.twitch.tier_vip.can_request is False

    _set_typed(cfg.audio, "output_device", "")     # empty means "unset"
    assert cfg.audio.output_device is None

    _set_typed(cfg.audio, "output_device", "3")
    assert cfg.audio.output_device == 3
