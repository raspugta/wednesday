"""Configuration loading and well-known paths.

Layout (XDG):
  code     : the repository (this file's grandparent)
  config   : ~/.config/wednesday/{config.yaml,permissions.yaml}   (user overrides)
  data     : ~/.local/share/wednesday/{models,runtime,voices,memory.db}
  state    : ~/.local/state/wednesday/{logs,token,pids,ui.json}
"""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
DEFAULT_CONFIG = PROJECT_DIR / "config" / "default.yaml"
DEFAULT_PERMISSIONS = PROJECT_DIR / "config" / "permissions.yaml"


def _xdg(var: str, fallback: str) -> Path:
    return Path(os.environ.get(var) or Path.home() / fallback)


@dataclass(frozen=True)
class Paths:
    config_dir: Path
    data_dir: Path
    state_dir: Path

    @classmethod
    def default(cls) -> "Paths":
        return cls(
            config_dir=_xdg("XDG_CONFIG_HOME", ".config") / "wednesday",
            data_dir=_xdg("XDG_DATA_HOME", ".local/share") / "wednesday",
            state_dir=_xdg("XDG_STATE_HOME", ".local/state") / "wednesday",
        )

    @property
    def log_dir(self) -> Path:
        return self.state_dir / "logs"

    @property
    def token_file(self) -> Path:
        return self.state_dir / "token"

    @property
    def db_file(self) -> Path:
        return self.data_dir / "memory.db"

    @property
    def tmp_dir(self) -> Path:
        return self.state_dir / "tmp"

    def ensure(self) -> None:
        for p in (self.config_dir, self.data_dir, self.state_dir, self.log_dir, self.tmp_dir):
            p.mkdir(parents=True, exist_ok=True)
        # State holds the API token and transient audio: keep it private.
        os.chmod(self.state_dir, 0o700)
        os.chmod(self.tmp_dir, 0o700)


def deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


class Config:
    """Dict-backed config with dotted lookups: cfg.get("model.ctx_size")."""

    def __init__(self, data: dict, permissions: dict, paths: Paths):
        self.data = data
        self.permissions = permissions
        self.paths = paths

    @classmethod
    def load(cls, paths: Paths | None = None) -> "Config":
        paths = paths or Paths.default()
        data = yaml.safe_load(DEFAULT_CONFIG.read_text()) or {}
        user_cfg = paths.config_dir / "config.yaml"
        if user_cfg.exists():
            data = deep_merge(data, yaml.safe_load(user_cfg.read_text()) or {})
        perm_file = paths.config_dir / "permissions.yaml"
        if not perm_file.exists():
            perm_file = DEFAULT_PERMISSIONS
        permissions = yaml.safe_load(perm_file.read_text()) or {}
        return cls(data, permissions, paths)

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, dotted: str, value: Any) -> None:
        parts = dotted.split(".")
        node = self.data
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value

    def data_path(self, rel: str) -> Path:
        p = Path(os.path.expanduser(rel))
        return p if p.is_absolute() else self.paths.data_dir / p

    @property
    def model_profile(self) -> dict:
        name = self.get("model.active")
        profiles = self.get("model.profiles", {})
        if name not in profiles:
            raise KeyError(f"model.active={name!r} is not one of {sorted(profiles)}")
        return {"name": name, **profiles[name]}

    @property
    def base_url(self) -> str:
        return f"http://{self.get('server.host')}:{self.get('server.port')}"
