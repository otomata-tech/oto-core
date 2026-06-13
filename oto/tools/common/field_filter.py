"""
Reusable field-level filtering / redaction for connector responses.

Connector clients return plain dicts / lists of dicts. A `FieldFilter` walks
that structure and rewrites sensitive fields — masking IBANs, dropping salaries,
anonymizing names — *before* the data reaches the AI agent or any consumer.
Since oto-core is a public lib feeding MCP tools, this doubles as a guardrail
against leaking PII through tool outputs.

The filter is connector-agnostic: any client can accept a `field_filter` and
apply it to its responses. Rules are plain dicts (serialisable to YAML), so a
policy can live in `~/.otomata/config.yaml` and apply automatically via
`FieldFilter.from_config(service)`.

Usage:
    from oto.tools.common import FieldFilter

    f = FieldFilter(rules=[
        {"fields": ["iban", "bic", "rib"], "action": "mask", "keep_last": 4},
        {"fields": ["nom", "prenom", "name"], "action": "anonymize"},
        {"fields": ["salaire", "numeroSecu"], "action": "drop"},
    ])
    clean = f.apply(api_response)   # dict or list of dicts

    # Or load a policy block from ~/.otomata/config.yaml:
    f = FieldFilter.from_config("silae")
"""

import hashlib
from typing import Any, Optional

# Default replacement for masked values.
_MASK = "••••"


class FieldFilter:
    """Recursively redacts fields of a connector response by leaf key name."""

    def __init__(self, rules: Optional[list[dict]] = None, salt: Optional[str] = None):
        """
        Args:
            rules: List of rule dicts. Each rule has:
                - ``fields``: list of field (leaf key) names to match,
                  case-insensitive, at any depth.
                - ``action``: one of ``drop``/``remove``, ``mask``,
                  ``anonymize``, ``hash``.
                - ``keep_last`` / ``keep_first`` (int, ``mask`` only): keep N
                  trailing/leading chars in clear, mask the rest.
            salt: Optional salt mixed into ``anonymize``/``hash`` to defeat
                dictionary re-identification. Deterministic for a given salt.
        """
        self.salt = salt or ""
        # Flatten rules into a lookup: lower-cased field name -> rule.
        self._by_field: dict[str, dict] = {}
        for rule in rules or []:
            for name in rule.get("fields", []):
                self._by_field[str(name).lower()] = rule

    @property
    def is_empty(self) -> bool:
        """True when there are no rules (``apply`` is the identity)."""
        return not self._by_field

    @classmethod
    def from_config(cls, service: str) -> "FieldFilter":
        """
        Build a filter from the ``field_filters.<service>`` block of
        ``~/.otomata/config.yaml``. Returns an empty (no-op) filter when absent.

        Expected shape::

            field_filters:
              silae:
                salt: "my-salt"
                rules:
                  - { fields: ["iban", "bic"], action: mask, keep_last: 4 }
                  - { fields: ["nom", "prenom"], action: anonymize }
        """
        # Imported lazily to avoid a hard import cycle (config has no deps on
        # tools, but keep the surface minimal).
        from ...config import get_config_section

        cfg = get_config_section("field_filters", {}) or {}
        block = cfg.get(service) or {}
        return cls(rules=block.get("rules", []), salt=block.get("salt"))

    # --- Core ---

    def apply(self, data: Any) -> Any:
        """Return a redacted copy of ``data`` (dict, list, or scalar).

        Input is never mutated. With no rules, returns ``data`` unchanged.
        """
        if self.is_empty:
            return data
        return self._walk(data)

    def _walk(self, value: Any) -> Any:
        if isinstance(value, dict):
            out: dict[Any, Any] = {}
            for key, val in value.items():
                rule = (
                    self._by_field.get(key.lower())
                    if isinstance(key, str)
                    else None
                )
                if rule is not None:
                    action = rule.get("action", "mask")
                    if action in ("drop", "remove"):
                        continue  # omit the key entirely
                    out[key] = self._transform(val, rule, action)
                else:
                    out[key] = self._walk(val)
            return out
        if isinstance(value, list):
            return [self._walk(item) for item in value]
        return value

    def _transform(self, value: Any, rule: dict, action: str) -> Any:
        """Apply a non-drop action to a single matched value.

        Containers are still recursed so a matched key holding a dict/list gets
        its nested fields redacted rather than blindly stringified.
        """
        if isinstance(value, (dict, list)):
            return self._walk(value)
        if value is None:
            return None
        if action == "mask":
            return self._mask(value, rule)
        if action == "anonymize":
            return "person_" + self._digest(value)[:6]
        if action == "hash":
            return self._digest(value)
        # Unknown action — fail safe to a full mask rather than leaking.
        return _MASK

    def _mask(self, value: Any, rule: dict) -> str:
        s = str(value)
        keep_last = int(rule.get("keep_last", 0) or 0)
        keep_first = int(rule.get("keep_first", 0) or 0)
        if keep_last and len(s) > keep_last:
            return _MASK + s[-keep_last:]
        if keep_first and len(s) > keep_first:
            return s[:keep_first] + _MASK
        return _MASK

    def _digest(self, value: Any) -> str:
        return hashlib.sha256((self.salt + str(value)).encode("utf-8")).hexdigest()
