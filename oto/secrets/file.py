"""Local file secret provider (`.otomata/secrets.env`).

Reads plain ``KEY=value`` files: a project-scoped
``<cwd-or-parent>/.otomata/secrets.env`` (searched up to 4 levels up) takes
precedence over the user file ``~/.otomata/secrets.env``.

This provider is also the graceful fallback of the sops/scaleway providers when
their store is absent (see ``oto.config.get_secret``), so it never reports
``STORE_ABSENT`` — a missing file simply means the key is not defined here.
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict

from .base import MISSING

# Cache for parsed secrets files, keyed by path.
_secrets_cache: Dict[Path, Dict[str, str]] = {}


def _parse_env_file(path: Path) -> Dict[str, str]:
    """Parse a ``.env`` file into a dictionary. Cached per path."""
    if path in _secrets_cache:
        return _secrets_cache[path]

    result: Dict[str, str] = {}
    if path.exists():
        with open(path, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, value = line.split("=", 1)
                    value = value.strip()
                    if (value.startswith("'") and value.endswith("'")) or (
                        value.startswith('"') and value.endswith('"')
                    ):
                        value = value[1:-1]
                    result[key.strip()] = value

    _secrets_cache[path] = result
    return result


def _find_project_secrets() -> "Path | None":
    """Find ``.otomata/secrets.env`` in CWD or up to 4 parent directories."""
    cwd = Path.cwd()
    for _ in range(5):
        secrets_file = cwd / ".otomata" / "secrets.env"
        if secrets_file.exists():
            return secrets_file
        if cwd.parent == cwd:
            break
        cwd = cwd.parent
    return None


def _user_secrets() -> Path:
    """User secrets file path (``~/.otomata/secrets.env``)."""
    return Path.home() / ".otomata" / "secrets.env"


class FileProvider:
    """Resolve secrets from project- then user-scoped ``.env`` files."""

    def lookup(self, name: str) -> object:
        project_secrets = _find_project_secrets()
        if project_secrets:
            secrets = _parse_env_file(project_secrets)
            if name in secrets:
                return secrets[name]
        secrets = _parse_env_file(_user_secrets())
        if name in secrets:
            return secrets[name]
        return MISSING
