"""Secret providers + factory for oto secret resolution.

`oto.config.get_secret` is the orchestrator (env → provider → file fallback →
default); this package holds the pluggable backing stores behind a uniform
:class:`~oto.secrets.base.SecretProvider` interface, selected by
:func:`make_provider`.

    file      → project/user `.otomata/secrets.env`
    sops      → SOPS+age encrypted YAML store (default)
    scaleway  → Scaleway Secret Manager

Adding a store = a new module exposing `lookup(name)` + `store_exists()` and
one line in the registry below — no branching in `oto.config`.
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

    Raises on an unknown name — it used to fall back silently to the local
    file provider, which turned a typo'd `secret_provider` (e.g. `sop`) into
    "every secret resolves to its default", indistinguishable from a store
    that is legitimately empty (oto-core#63). A misconfigured provider name is
    not a missing store: it deserves a loud, named failure, not a quiet swap
    to a different backing store the caller never asked for.
    """
    try:
        builder = _REGISTRY[name]
    except KeyError:
        raise ValueError(
            f"Unknown secret provider {name!r} (from `secret_provider` in "
            f"~/.otomata/config.yaml). Valid values: "
            f"{', '.join(sorted(_REGISTRY))}. Fix it with "
            f"`oto config provider secrets <name>`."
        ) from None
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
