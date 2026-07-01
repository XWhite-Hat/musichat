"""
data_dir.py — single importable source of truth for the user data directory.

bootstrap_check.run() sets MUSICHAT_DATA_DIR in os.environ before any other
module is imported.  All path-using modules read DATA_DIR from here so there
is exactly one place to update when the user moves their data folder.

In development (python main.py directly, not a frozen binary) the env var is
never set, so this falls back to the legacy default location — no migration
required for developers.
"""
import os

_DEFAULT = os.path.join(os.path.expanduser("~"), ".streamdeck_music")
DATA_DIR: str = os.environ.get("MUSICHAT_DATA_DIR", _DEFAULT)
