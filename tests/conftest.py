import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from wednesday.config import Config, Paths  # noqa: E402


@pytest.fixture
def tmp_cfg(tmp_path):
    """Config whose config/data/state dirs live in a temp dir (never touches the real install)."""
    paths = Paths(tmp_path / "config", tmp_path / "data", tmp_path / "state")
    cfg = Config.load(paths)
    paths.ensure()
    return cfg


@pytest.fixture
def perms(tmp_path):
    from wednesday.agent.permissions import Permissions
    home = tmp_path / "home"
    (home / "proj").mkdir(parents=True)
    (home / ".ssh").mkdir()
    (home / ".ssh" / "id_rsa").write_text("SECRET")
    (home / "notes.txt").write_text("hello\nworld\n")
    (home / "proj" / ".env").write_text("API_KEY=x")
    policy = {
        "read_roots": ["~"],
        "write_roots": ["~/proj"],
        "deny_paths": ["~/.ssh", "~/.config/wednesday"],
        "deny_name_patterns": [".env", "*.pem", "id_rsa*"],
        "commands": {
            "auto_allow": ["ls", "cat", "git", "echo", "find", "grep", "head", "python3"],
            "readonly_subcommands": {"git": ["status", "log", "diff", "branch"], "python3": ["--version"]},
            "blocked": ["sudo", "dd", "mkfs*", "shutdown", "env"],
        },
    }
    return Permissions(policy, home=home), home
