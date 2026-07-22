"""Secret provider protocol + resolution sentinels.

A provider resolves ONE secret name against its backing store. `lookup` returns:

- the value (found);
- ``MISSING`` — the store exists but has no such key. The caller stops here and
  returns its default (NO fallback: an existing store that lacks a key is an
  authoritative "not defined");
- ``STORE_ABSENT`` — the store itself is missing (e.g. a fresh third-party
  install with the ``sops`` default but no SOPS repo cloned). The caller MAY
  fall back to the local file provider.

Keeping these two "not found" cases distinct is what lets `get_secret` fall back
gracefully on a missing store without masking a deliberately-absent key.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

# Sentinels — identity-compared, never returned to callers of get_secret.
MISSING = object()
STORE_ABSENT = object()


class AmbiguousSecretError(RuntimeError):
    """The key exists in the vault but with different values across files.

    Returning one of the values would be arbitrary (the key is scoped per
    project/mission file, not transverse), so resolution raises instead.
    """


@runtime_checkable
class SecretProvider(Protocol):
    """A backing store that can resolve a single secret by name."""

    def lookup(self, name: str) -> object:
        """Return the value, or ``MISSING`` / ``STORE_ABSENT``."""
        ...
