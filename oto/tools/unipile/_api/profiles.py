"""Fiches membre & société, avec la garde anti-mismatch #153.

Extrait de `client.py` (découpage par domaine, surface publique figée) :
les corps sont inchangés. Ce mixin n'est jamais instancié seul — il est
composé dans `UnipileClient`, qui fournit le transport (`_request`,
`_acct`, `_norm`, `_by_shape`, `session`).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from ..const import _SCRAPE_TIMEOUT, _sections_param, _slug_from_company_url
from ..errors import UnipileError


class _ProfilesMixin:
    """Fiches membre & société, avec la garde anti-mismatch #153."""

    @staticmethod
    def _identity_ok(requested: str, resp: dict, expect_object: str) -> bool:
        """True si `resp` correspond bien au type ET à l'identifiant demandés.
        Tolère slug↔id (compare requested à `public_identifier`, `id`,
        `provider_id`, insensible à la casse)."""
        if not isinstance(resp, dict):
            return False
        obj = resp.get("object")
        if obj and expect_object and obj != expect_object:
            return False  # ex. demandé UserProfile, reçu CompanyProfile (#148/#149)
        req = str(requested).strip().lower()
        cands = {
            str(resp.get(k, "")).strip().lower()
            for k in ("public_identifier", "id", "provider_id", "member_urn")
        }
        return req in cands if any(cands) else True  # pas d'id à comparer → on laisse

    def get_profile(self, identifier: str, sections: str = "*") -> dict:
        """Profil complet. `identifier` = public identifier (slug) ou provider id.

        Garde #153 : rejette une réponse qui ne correspond pas au membre demandé
        (mauvais appariement observé sous concurrence) → `UnipileError` retryable."""
        params: dict[str, Any] = {}
        secs = _sections_param(sections)
        if secs:
            params["with_sections"] = secs
        data = self._request(
            "GET", self._acct(f"/users/{quote(identifier, safe='')}"), params=params
        )
        if not self._identity_ok(identifier, data, "UserProfile"):
            got = (data or {}).get("public_identifier") or (data or {}).get("id")
            raise UnipileError(
                f"Unipile identity_mismatch: profil demandé {identifier!r}, "
                f"reçu {got!r} (object={(data or {}).get('object')!r}). "
                "Réponse rejetée — réessaie."
            )
        return data

    def _get_company_raw(self, identifier: str) -> dict:
        """GET société brut + garde anti-mismatch #153. Lève telle quelle
        (404 inclus) — le fallback de résolution vit dans `get_company`."""
        data = self._request(
            "GET", self._acct(f"/linkedin/company/{quote(identifier, safe='')}"),
            timeout=_SCRAPE_TIMEOUT,
        )
        if not self._identity_ok(identifier, data, "CompanyProfile"):
            got = (data or {}).get("public_identifier") or (data or {}).get("id")
            raise UnipileError(
                f"Unipile identity_mismatch: société demandée {identifier!r}, "
                f"reçu {got!r} (object={(data or {}).get('object')!r}). "
                "Réponse rejetée — réessaie."
            )
        return data

    def _resolve_company_slugs(self, name: str, limit: int = 5) -> list[str]:
        """#176 : cherche des sociétés par nom → `public_identifier` candidats,
        par ordre de pertinence. Best-effort : ne doit jamais masquer le 404
        d'origine (toute erreur de recherche → aucun candidat)."""
        try:
            res = self.search(category="companies", keywords=name)
        except Exception:  # noqa: BLE001 — résolution best-effort, jamais fatale
            return []
        items = (res or {}).get("items") or (res or {}).get("data") or []
        out: list[str] = []
        for it in items[:limit]:
            if not isinstance(it, dict):
                continue
            slug = it.get("public_identifier") or _slug_from_company_url(
                it.get("public_profile_url") or it.get("profile_url") or ""
            )
            if slug:
                out.append(slug)
        return list(dict.fromkeys(out))  # dédup en conservant l'ordre

    def get_company(self, identifier: str, resolve: bool = True) -> dict:
        """Fiche société. `identifier` = slug (`public_identifier`) ou id numérique.

        Garde #153 : rejette une réponse d'un autre objet/identifiant.
        Résolution tolérante #176 : si le slug fourni est introuvable (404) et
        non numérique, on tente une recherche société par nom pour retrouver le
        `public_identifier` canonique (ex. `mooniz` → `mooniz1`) et on réessaie.
        Échec → 404 propre enrichi des candidats proches (`resolve=False` coupe
        le fallback)."""
        try:
            return self._get_company_raw(identifier)
        except UnipileError as e:
            ident = str(identifier).strip()
            if not resolve or e.status_code != 404 or ident.isdigit():
                raise
            candidates = self._resolve_company_slugs(ident)
            for slug in candidates:
                if slug.strip().lower() != ident.lower():
                    try:
                        return self._get_company_raw(slug)
                    except UnipileError:
                        continue
            if candidates:
                raise UnipileError(
                    f"Unipile 404: société {identifier!r} introuvable. "
                    f"Slugs candidats proches : {', '.join(candidates)}.",
                    status_code=404,
                ) from e
            raise

