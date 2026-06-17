"""Pytest bootstrap: put the project root on sys.path so `tests/` can `import test_tool`."""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
