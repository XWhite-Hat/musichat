"""
launcher.py — PyInstaller entry point for the frozen binary.

1. Run the pre-PySide6 bootstrap (data dir check, first-run wizard, sys.path).
2. Import and run main().

In development (python main.py) this file is not used — main.py is run
directly and PySide6 is available from the venv.
"""
import sys

import bootstrap_check
bootstrap_check.run()

# PySide6 is now on sys.path (or present in venv for dev mode).
# Importing main triggers its module-level PySide6 imports.
#
# Call main.run(), not main.main() — run() installs the crash-log handlers.
# Importing main() directly bypassed them entirely (main.py's __name__ is
# "main" here, not "__main__"), which is why released builds never produced
# a crash.log.
from main import run

sys.exit(run())
