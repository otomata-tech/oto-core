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
        {"fields": ["email"], "action": "mask", "preserve": "email"},
        {"fields": ["nom", "prenom", "name"], "action": "pseudonym", "kind": "name"},
        {"fields": ["dateNaissance"], "action": "generalize", "to": "year"},
        {"fields": ["salaire", "numeroSecu"], "action": "drop"},
    ])
    clean = f.apply(api_response)   # dict or list of dicts

    # Or load a policy block from ~/.otomata/config.yaml:
    f = FieldFilter.from_config("silae")
"""

import hashlib
import re
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
                  ``anonymize``, ``hash``, ``pseudonym``, ``generalize``.
                - ``keep_last`` / ``keep_first`` (int, ``mask`` only): keep N
                  trailing/leading chars in clear, mask the rest.
                - ``preserve`` (``mask`` only): ``email``/``phone``/``iban`` —
                  format-preserving mask that keeps the recognisable parts.
                - ``kind`` (``pseudonym`` only): ``name``/``first_name``/
                  ``last_name``/``email``/``company``/``phone``/``address``.
                - ``to`` (``generalize`` only): ``year``/``month``/``range``/
                  ``department`` ; ``step`` (int) for ``range``.
            salt: Optional salt mixed into ``anonymize``/``hash``/``pseudonym``
                to defeat dictionary re-identification. Deterministic for a
                given salt.
        """
        self.salt = salt or ""
        # Flatten rules into a lookup: lower-cased field name -> rule.
        self._by_field: dict[str, dict] = {}
        for rule in rules or []:
            for name in rule.get("fields", []):
                self._by_field[str(name).lower()] = rule
        # Lazily built Faker instance (``pseudonym`` mode only).
        self._faker = None

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
        if action == "pseudonym":
            return self._pseudonym(value, rule)
        if action == "generalize":
            return self._generalize(value, rule)
        # Unknown action — fail safe to a full mask rather than leaking.
        return _MASK

    # --- Actions ---

    def _mask(self, value: Any, rule: dict) -> str:
        s = str(value)
        preserve = rule.get("preserve")
        if preserve == "email":
            return self._mask_email(s)
        if preserve == "phone":
            return self._mask_phone(s)
        if preserve == "iban":
            return self._mask_iban(s)
        keep_last = int(rule.get("keep_last", 0) or 0)
        keep_first = int(rule.get("keep_first", 0) or 0)
        if keep_last and len(s) > keep_last:
            return _MASK + s[-keep_last:]
        if keep_first and len(s) > keep_first:
            return s[:keep_first] + _MASK
        return _MASK

    @staticmethod
    def _mask_email(s: str) -> str:
        """``jean.dupont@acme.com`` -> ``j••••@acme.com`` (keeps the domain)."""
        local, sep, domain = s.partition("@")
        if not sep:
            return _MASK
        head = local[:1] if local else ""
        return f"{head}{_MASK}@{domain}"

    @staticmethod
    def _mask_phone(s: str) -> str:
        """Keep the country code (if any) and the last 2 digits, mask the rest."""
        digits = re.sub(r"\D", "", s)
        if len(digits) < 2:
            return _MASK
        prefix = f"+{digits[:2]} " if s.strip().startswith("+") else ""
        return f"{prefix}{_MASK} {digits[-2:]}"

    @staticmethod
    def _mask_iban(s: str) -> str:
        """Keep the country prefix and the last 4 chars (``FR••••3456``)."""
        compact = re.sub(r"\s", "", s)
        if len(compact) < 6:
            return _MASK
        return f"{compact[:2]}{_MASK}{compact[-4:]}"

    def _pseudonym(self, value: Any, rule: dict) -> str:
        """Replace with a realistic fake value, stable for a given input.

        Uses Faker seeded by ``digest(salt+value)`` so the same source value
        always maps to the same pseudonym (coherent across the response).
        """
        faker = self._get_faker()
        kind = rule.get("kind", "name")
        faker.seed_instance(int(self._digest(value)[:12], 16))
        generator = getattr(faker, kind, None)
        if generator is None:
            # Unknown kind — fall back to a generic name rather than leaking.
            generator = faker.name
        return str(generator())

    def _generalize(self, value: Any, rule: dict) -> Any:
        """Reduce precision: date->year/month, postal code->department, number->range."""
        to = rule.get("to", "year")
        s = str(value)
        if to == "year":
            return s[:4]
        if to == "month":
            return s[:7]
        if to == "department":
            digits = re.sub(r"\D", "", s)
            return digits[:2] if len(digits) >= 2 else _MASK
        if to == "range":
            step = int(rule.get("step", 1000) or 1000)
            try:
                n = float(value)
            except (TypeError, ValueError):
                return _MASK
            low = int(n // step) * step
            return f"{low}-{low + step}"
        return _MASK

    # --- Helpers ---

    def _get_faker(self):
        if self._faker is None:
            try:
                from faker import Faker
            except ImportError as e:  # pragma: no cover - depends on extra
                raise RuntimeError(
                    "Le mode 'pseudonym' requiert Faker. Installez l'extra : "
                    "pip install 'oto-core[anonymize]'."
                ) from e
            self._faker = Faker("fr_FR")
        return self._faker

    def _digest(self, value: Any) -> str:
        return hashlib.sha256((self.salt + str(value)).encode("utf-8")).hexdigest()
