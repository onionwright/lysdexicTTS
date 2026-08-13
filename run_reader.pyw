"""Console-less launcher.

Run with ``pythonw.exe`` so no console window flashes at logon; this is the
target of the HKCU Run registry value that the tray's "Start with Windows"
option installs.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# pythonw.exe gives the process no stdout/stderr at all. Several libraries --
# kokoro's loguru setup among them -- assume those exist and raise at import
# time if they are None, so they must be replaced before anything else loads.
from reader.log import ensure_std_streams  # noqa: E402

ensure_std_streams()

from reader.app import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
