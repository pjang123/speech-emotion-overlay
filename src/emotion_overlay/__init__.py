"""Real-time speech emotion -> user image overlay."""

import os
import sys

__version__ = "0.1.0"

if sys.platform == "darwin":
    # On a Mac's GPU (PyTorch "mps"), run any operation Metal doesn't support yet on
    # the CPU instead of failing. PyTorch reads this once, when it's first imported,
    # and nothing imports it before this package.
    os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
