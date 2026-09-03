"""Entreprises Productlane — et leur jumelage avec les « customers » Linear.

Ce mixin n'est jamais instancié seul : il est composé dans `ProductlaneClient`,
qui fournit le transport (`_request`, `_list`).

⚠️ **Le miroir Linear est ASYNCHRONE** : créer une entreprise provisionne un
customer Linear une fois qu'un domaine est posé, une mise à jour d'identité s'y
propage plus tard, et une suppression y supprime le customer après coup. Une
lecture immédiate côté Linear peut donc ne rien montrer sans que rien n'ait
échoué — c'est un délai, pas une panne.
"""
from __future__ import annotations

from typing import Any, Dict, Optional


class _CompaniesMixin:
    """Entreprises."""

    def list_companies(self, limit: Optional[int] = None,
                       cursor: Optional[str] = None,
                       external_id: Optional[str] = None,
                       domain: Optional[str] = None,
                       name_contains: Optional[str] = None,
                       status_id: Optional[str] = None,
                       tier_id: Optional[str] = None,
                       size_gte: Optional[Any] = None,
                       size_lte: Optional[Any] = None,
                       revenue_gte: Optional[Any] = None,
                       revenue_lte: Optional[Any] = None,
                       created_after: Optional[str] = None,
                       created_before: Optional[str] = None,
                       updated_after: Optional[str] = None,
                       updated_before: Optional[str] = None) -> Any:
        """GET /companies — entreprises de l'espace de travail. Scope `companies:read`.

        `size_*` et `revenue_*` sont des bornes inclusives (`gte`/`lte`), et
        `status_id`/`tier_id` renvoient aux options Linear (cf.
        `linear_customer_options`).
        """
        return self._list("/companies", limit, cursor, {
            "external_id": external_id, "domain": domain,
            "name_contains": name_contains, "status_id": status_id,
            "tier_id": tier_id, "size_gte": size_gte, "size_lte": size_lte,
            "revenue_gte": revenue_gte, "revenue_lte": revenue_lte,
            "created_after": created_after, "created_before": created_before,
            "updated_after": updated_after, "updated_before": updated_before,
        })

    def get_company(self, company_id: str) -> Any:
        """GET /companies/{id} — une entreprise. Scope `companies:read`."""
        return self._request("GET", f"/companies/{company_id}")

    def create_company(self, payload: Dict[str, Any]) -> Any:
        """POST /companies — crée une entreprise. Scope `companies:write`.

        Requis : `name`. Optionnels : `logo_url`, `domains`, `size`, `revenue`,
        `external_ids`, `status_id`, `tier_id`, `owner_id`.

        Le customer Linear est provisionné **de façon asynchrone**, et seulement
        une fois qu'un domaine est renseigné.
        """
        return self._request("POST", "/companies", json=dict(payload))

    def update_company(self, company_id: str, payload: Dict[str, Any]) -> Any:
        """PATCH /companies/{id} — met à jour une entreprise. Scope `companies:write`.

        Champs : `name`, `logo_url`, `domains`, `size`, `revenue`,
        `external_ids`, `status_id`, `tier_id`, `owner_id`. Les champs d'identité
        sont poussés vers Linear **de façon asynchrone**.
        """
        return self._request("PATCH", f"/companies/{company_id}",
                             json=dict(payload))

    def delete_company(self, company_id: str) -> Any:
        """DELETE /companies/{id} — **soft-delete**. Scope `companies:write`.

        Le customer Linear est supprimé de façon asynchrone, et les fils qui
        perdent leur lien d'entreprise sont réindexés ensuite.
        """
        return self._request("DELETE", f"/companies/{company_id}")

    def merge_company(self, company_id: str, source_id: str) -> Any:
        """POST /companies/{id}/merge — fusionne `source_id` DANS `company_id`.

        Scope `companies:write`.

        ⚠️ **Irréversible, et le sens compte** : l'entreprise du CHEMIN survit,
        celle de `source_id` est supprimée. Ses fils, contacts et votes sont
        déplacés vers la survivante, dont les propriétés vides sont complétées
        par celles de la source (les propriétés déjà remplies ne bougent pas).
        Si les deux ont un customer Linear, ils sont fusionnés aussi.
        """
        if not source_id:
            raise ValueError(
                "`source_id` requis : c'est l'entreprise ABSORBÉE (celle du "
                "chemin survit).")
        if source_id == company_id:
            raise ValueError(
                "fusionner une entreprise avec elle-même : `source_id` doit "
                "différer de l'entreprise du chemin.")
        return self._request("POST", f"/companies/{company_id}/merge",
                             json={"source_id": source_id})

    def linear_customer_options(self, team_id: Optional[str] = None) -> Any:
        """GET /companies/linear-options — statuts et tiers Linear disponibles.

        Rend `null` si Linear n'est pas connecté — ce n'est donc pas une erreur,
        mais la réponse à « ce workspace a-t-il Linear ? ». Sert à remplir
        `status_id` / `tier_id`.
        """
        return self._request("GET", "/companies/linear-options",
                             params={"team_id": team_id})
