"""
Config persistence.

Covers the failure chain that could wipe every setting a user had: a truncating
write that leaves a half-file if the app dies mid-save, no lock despite writes
arriving from three threads, and a load path that silently swallowed the
resulting corruption and returned defaults.
"""
from __future__ import annotations

import json
import os
import threading

import config


def test_round_trip_preserves_values(data_dir):
    cfg = config.load_config()
    cfg.twitch.prefix = "?"
    cfg.twitch.tier_viewer.queue_limit = 7
    config.save_config(cfg)

    reloaded = config.load_config()
    assert reloaded.twitch.prefix == "?"
    assert reloaded.twitch.tier_viewer.queue_limit == 7


def test_save_leaves_no_temp_file_behind(data_dir):
    cfg = config.load_config()
    config.save_config(cfg)
    assert not os.path.exists(config.CONFIG_PATH + ".tmp")


def test_saved_file_is_valid_json(data_dir):
    cfg = config.load_config()
    config.save_config(cfg)
    with open(config.CONFIG_PATH, encoding="utf-8") as fh:
        assert isinstance(json.load(fh), dict)


def test_existing_config_survives_a_failed_write(data_dir, monkeypatch):
    """
    The point of writing to a temp file and renaming: if the write dies partway
    through, the previous config must still be on disk and readable.
    """
    cfg = config.load_config()
    cfg.twitch.prefix = "!"
    config.save_config(cfg)

    real_replace = os.replace

    def boom(src, dst):
        raise OSError("simulated crash between write and rename")

    monkeypatch.setattr(os, "replace", boom)
    cfg.twitch.prefix = "@"
    try:
        config.save_config(cfg)
    except OSError:
        pass
    monkeypatch.setattr(os, "replace", real_replace)

    # Old config intact, not truncated to nothing.
    assert config.load_config().twitch.prefix == "!"


def test_corrupt_config_is_preserved_not_discarded(data_dir):
    cfg = config.load_config()
    config.save_config(cfg)

    with open(config.CONFIG_PATH, "w", encoding="utf-8") as fh:
        fh.write("{ not valid json at all")

    loaded = config.load_config()
    assert loaded.twitch.prefix == "!"                     # defaults returned
    assert os.path.exists(config.CONFIG_PATH + ".corrupt")  # evidence kept


def test_concurrent_saves_never_corrupt_the_file(data_dir):
    """save_config is reachable from the Qt, settings-server and bot threads."""
    cfg = config.load_config()
    config.save_config(cfg)

    errors: list[Exception] = []

    def writer(n: int) -> None:
        try:
            for _ in range(15):
                local = config.load_config()
                local.twitch.prefix = "!?@#$"[n % 5]
                config.save_config(local)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    with open(config.CONFIG_PATH, encoding="utf-8") as fh:
        json.load(fh)   # must still parse — a torn write would raise here
    assert not os.path.exists(config.CONFIG_PATH + ".corrupt")
