"""Keel path helper — single place where the workspace root resolves.

The root is KEEL_HOME, defaulting to ~/keel. No module may
hardcode the private pipeline's workspace path. Import this instead.
"""
import os

HOME = os.environ.get("KEEL_HOME", os.path.expanduser("~/keel"))
ENGINES = os.path.join(HOME, "engines")
DATA = os.path.join(HOME, "data")
TELEMETRY = os.path.join(DATA, "telemetry")
