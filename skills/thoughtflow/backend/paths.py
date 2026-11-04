"""Local state lives outside the installed plugin so updates do not erase it."""
import os
import sys
from pathlib import Path

def runtime_dir():
    override = os.environ.get("THOUGHT_FLOW_DATA_DIR")
    if override:
        return Path(override).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/ThoughtFlow"
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "ThoughtFlow"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "thought-flow"
