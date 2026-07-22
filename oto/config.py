"""Configuration loader for otomata tools.

Secret resolution order:
1. Environment variable (always)
2. Configured provider (sops, file, or scaleway)
3. Default value
"""

import os
import json
from pathlib import Path
from typing import Optional, Dict, Any

from oto.secrets import (
    MISSING,
    STORE_ABSENT,
    AmbiguousSecretError,
    FileProvider,
    make_provider,
)

# Re-exported for backwards compatibility: `from oto.config import AmbiguousSecretError`
# keeps working now that the exception lives in oto.secrets.base.
__all__ = ["get_secret", "get_json_secret", "require_secret", "AmbiguousSecretError"]

_oto_config_cache: Optional[Dict[str, Any]] = None


def _get_oto_config() -> Dict[str, Any]:
    """Read ~/.otomata/config.yaml. Cached."""
    global _oto_config_cache
    if _oto_config_cache is not None:
        return _oto_config_cache

    config_file = Path.home() / ".otomata" / "config.yaml"
    if config_file.exists():
        import yaml
        with open(config_file) as f:
            _oto_config_cache = yaml.safe_load(f) or {}
    else:
        _oto_config_cache = {}
    return _oto_config_cache


def get_config_section(key: str, default: Any = None) -> Any:
    """Read a named block from ~/.otomata/config.yaml.

    Generic accessor for feature config that lives alongside `secret_provider`
    / `search_provider` (e.g. `field_filters`). Returns `default` when absent.
    """
    return _get_oto_config().get(key, default)


def get_provider() -> str:
    """Return configured secret provider ('sops', 'file', or 'scaleway').

    `sops` is the new default — secrets decrypted on demand from a SOPS
    YAML file (see `oto.secrets.sops`). `file` and `scaleway` are kept for
    backwards compat and migration.
    """
    return _get_oto_config().get("secret_provider", "sops")


def write_oto_config(config: Dict[str, Any]) -> None:
    """Write ~/.otomata/config.yaml."""
    global _oto_config_cache
    import yaml
    config_file = get_config_dir() / "config.yaml"
    with open(config_file, "w") as f:
        yaml.dump(config, f, default_flow_style=False)
    _oto_config_cache = config


def get_search_provider() -> str:
    """Return configured search provider ('serper' or 'browser')."""
    return _get_oto_config().get("search_provider", "serper")


def get_secret(name: str, default: Optional[str] = None) -> Optional[str]:
    """
    Get a secret value.

    Resolution order:
    1. Environment variable (always, highest priority)
    2. Configured provider (sops / scaleway / file), via `oto.secrets.make_provider`
    3. Local file provider as a graceful fallback when the configured provider
       has no backing store (e.g. fresh/third-party install with sops default
       but no SOPS repo cloned)
    4. Default value

    `get_secret` is the soft accessor: it honours its contract and returns
    `default` when a secret can't be resolved — it never raises just because a
    provider's store is absent. The hard failure (with guidance) belongs to
    `require_secret`.

    Exception: a key present in the vault with DIFFERENT values across files
    raises AmbiguousSecretError — returning one of them would be arbitrary.

    Args:
        name: Secret name (e.g., 'GROQ_API_KEY', 'SIRENE_API_KEY')
        default: Default value if not found

    Returns:
        Secret value or default
    """
    # 1. Environment variable (always)
    env_val = os.environ.get(name)
    if env_val:
        return env_val

    # Server hardening (oto-mcp#12) : OTO_CONFIG_DISABLE_SOPS=1 ⇒ le process ne
    # résout QUE son environnement — ni SOPS, ni ~/.otomata/secrets.env. Les
    # serveurs (oto-mcp) tirent leurs credentials de leur propre store (DB,
    # injection) ; une lecture filesystem silencieuse ici contournerait ça.
    if os.environ.get("OTO_CONFIG_DISABLE_SOPS") == "1":
        return default

    # 2. Configured provider (may raise AmbiguousSecretError).
    provider = make_provider(get_provider(), _get_oto_config())
    value = provider.lookup(name)
    if value is not MISSING and value is not STORE_ABSENT:
        return value

    # 3. Graceful fallback to local file secrets ONLY when the configured
    #    provider's store is absent (sops default but no SOPS repo on a
    #    third-party box). A store that is present but lacks the key is
    #    authoritative — no fallback.
    if value is STORE_ABSENT:
        value = FileProvider().lookup(name)
        if value is not MISSING:
            return value

    return default


def get_json_secret(name: str) -> Optional[Dict[str, Any]]:
    """
    Get a secret that contains JSON data.

    Args:
        name: Secret name

    Returns:
        Parsed JSON as dictionary, or None if not found
    """
    value = get_secret(name)
    if value:
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return None
    return None


def require_secret(name: str) -> str:
    """
    Get a required secret, raise error if not found.

    Args:
        name: Secret name

    Returns:
        Secret value

    Raises:
        ValueError: If secret not found
    """
    value = get_secret(name)
    if value is None:
        if os.environ.get("OTO_CONFIG_DISABLE_SOPS") == "1":
            raise ValueError(
                f"Required secret '{name}' not found — filesystem secret stores are "
                f"disabled (OTO_CONFIG_DISABLE_SOPS=1, server mode). Provide it via "
                f"the process environment or the service's credential store (DB)."
            )
        provider = get_provider()
        raise ValueError(
            f"Required secret '{name}' not found. Set it via:\n"
            f"  - Environment variable: export {name}='...'  (always wins, simplest)\n"
            f"  - Local file provider: `oto config provider secrets file`, then add\n"
            f"    {name}=... to ~/.otomata/secrets.env\n"
            f"  - SOPS provider (otomata infra): keep `secret_provider: sops` and add the\n"
            f"    key to your SOPS store\n"
            f"  (current provider: {provider})"
        )
    return value


def get_config_dir() -> Path:
    """Get otomata config directory (~/.otomata/)."""
    config_dir = Path.home() / ".otomata"
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir


def get_cache_dir() -> Path:
    """Get otomata cache directory (~/.cache/otomata/)."""
    cache_dir = Path.home() / ".cache" / "otomata"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def get_sessions_dir() -> Path:
    """Get browser sessions directory (~/.otomata/sessions/)."""
    sessions_dir = get_config_dir() / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    return sessions_dir
