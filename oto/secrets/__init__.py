"""Secret providers + factory for oto secret resolution.

`oto.config.get_secret` is the orchestrator (env → provider → file fallback →
default); this package holds the pluggable backing stores behind a uniform
:class:`~oto.secrets.base.SecretProvider` interface, selected by
:func:`make_provider`.

    file      → project/user `.otomata/secrets.env`
    sops      → SOPS+age encrypted YAML store (default)
    scaleway  → Scaleway Secret Manager

Adding a store = a new module exposing a `lookup(name)` and one line in the
registry below — no branching in `oto.config`.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from .base import MISSING, STORE_ABSENT, AmbiguousSecretError, SecretProvider
from .file import FileProvider
from .scaleway import ScalewayProvider
from .sops import SopsProvider

# provider name → builder. A builder takes the parsed ~/.otomata/config.yaml
# (so a provider can read its own settings, e.g. sops_dir/sops_file) and
# returns a SecretProvider.
_REGISTRY: Dict[str, Callable[[Dict[str, Any]], SecretProvider]] = {
    "file": lambda cfg: FileProvider(),
    "sops": lambda cfg: SopsProvider(cfg),
    "scaleway": lambda cfg: ScalewayProvider(),
}


def make_provider(name: str, cfg: Optional[Dict[str, Any]] = None) -> SecretProvider:
    """Build the secret provider for `name`.

    Unknown names fall back to the local file provider (the safe default for a
    fresh/third-party install), matching the historical behaviour.
    """
    builder = _REGISTRY.get(name, _REGISTRY["file"])
    return builder(cfg or {})


__all__ = [
    "MISSING",
    "STORE_ABSENT",
    "AmbiguousSecretError",
    "SecretProvider",
    "FileProvider",
    "SopsProvider",
    "ScalewayProvider",
    "make_provider",
]
