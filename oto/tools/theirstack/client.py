"""TheirStack API client — offres d'emploi par employeur + technologies utilisées.

API v1 (https://api.theirstack.com, doc https://theirstack.com/en/docs/api-reference),
auth **Bearer**. Deux surfaces de recherche, chacune un POST dont le corps EST la DSL
de filtres TheirStack (~110 champs pour les jobs, ~60 pour les entreprises) : le
client la passe telle quelle, il ne la re-type pas — l'appelant compose le dict, la
doc éditeur fait foi pour les noms de champs. Réponse = `{metadata, data}`.

- `search_jobs(payload)`      → POST /v1/jobs/search
- `search_companies(payload)` → POST /v1/companies/search
- `credit_balance()`          → GET  /v0/billing/credit-balance (gratuit ; sonde d'auth)

Filtres les plus utiles (extraits du spec OpenAPI du 17/08/2026) :
- pagination : `page` (0-based, défaut 0), `limit` (défaut 25), `offset`, `cursor` ;
- fraîcheur : `posted_at_max_age_days` (0 = aujourd'hui), `posted_at_gte`/`_lte`,
  `discovered_at_max_age_days` ;
- entreprise : `company_name_or` (exact, SENSIBLE à la casse), `company_name_case_insensitive_or`,
  `company_name_partial_match_or`, `company_domain_or`, `company_linkedin_url_or`,
  `company_country_code_or` (ISO2, pays du siège), `min_employee_count`/`max_employee_count`,
  `industry_or`, `company_technology_slug_or` ;
- offre : `job_title_or` (mots-clés), `job_title_pattern_or` (regex), `job_country_code_or`
  (ISO2), `job_location_pattern_or`, `job_seniority_or`, `job_technology_slug_or`,
  `job_description_pattern_or`, `workplace_types_or`, `employment_statuses_or` ;
- coût : `include_total_results` (lent, à ne poser qu'au 1er appel), `blur_company_data`
  (aperçu flouté, sans crédit — sur demande à TheirStack pour les nouveaux workspaces).

⚠️ `/v1/jobs/search` exige AU MOINS un de : `posted_at_max_age_days`, `posted_at_gte`,
`posted_at_lte`, `company_domain_or`, `company_linkedin_url_or`, `company_name_or` —
sinon l'API refuse (raison de performance). Rien n'est ajouté ici : le refus amont
remonte tel quel (`UpstreamHTTPError`, status 4xx), lisible par l'appelant.

Facturation (spec OpenAPI du 17/08/2026) : le crédit se compte au RECORD rendu —
1 crédit API par offre sur `/v1/jobs/search`, 3 par entreprise sur
`/v1/companies/search` ; `limit` borne donc la dépense. `metadata.truncated_results`
/ `truncated_companies` disent combien de résultats n'ont PAS été rendus faute de
crédits.

Requires: requests
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import requests

from ...config import require_secret
from ..common import raise_for_upstream

_HTTP_TIMEOUT = (10, 60)  # (connexion, lecture) — jamais d'attente illimitée


class TheirStackClient:
    """Client TheirStack v1 (https://api.theirstack.com), auth Bearer."""

    BASE_URL = "https://api.theirstack.com"

    def __init__(self, api_key: Optional[str] = None):
        """
        Args:
            api_key: clé TheirStack (ou variable d'env `THEIRSTACK_API_KEY`).
        """
        self.api_key = api_key or require_secret("THEIRSTACK_API_KEY")
        self.session = requests.Session()
        # La clé part en HEADER, jamais en query string (elle atterrirait dans l'URL,
        # donc dans le message de toute exception requests, les logs et Sentry).
        self.session.headers.update({
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        })

    # --- transport ----------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs) -> Any:
        resp = self.session.request(
            method, f"{self.BASE_URL}{path}", timeout=_HTTP_TIMEOUT, **kwargs)
        raise_for_upstream(resp, service="theirstack")
        return resp.json() if resp.content else {}

    @staticmethod
    def _payload(payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        if payload is None:
            return {}
        if not isinstance(payload, dict):
            raise ValueError("payload doit être un dict (la DSL de filtres TheirStack).")
        return dict(payload)

    # --- recherche ----------------------------------------------------------

    def search_jobs(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """POST /v1/jobs/search — offres d'emploi filtrées par la DSL TheirStack.

        `payload` = le corps tel que documenté par l'éditeur (`page`, `limit`,
        `posted_at_max_age_days`, `company_name_or`, `company_country_code_or`,
        `job_country_code_or`, `job_title_or`, `job_technology_slug_or`…). Renvoie
        `{metadata: {total_results?, truncated_results, truncated_companies,
        total_companies?}, data: [job…]}` — chaque job porte `company`, `job_title`,
        `date_posted`, `url`, `location`, `company_domain`, `technology_slugs`,
        `company_object`, `description`…

        Coût : 1 crédit API par offre rendue → borner `limit`.
        """
        return self._request("POST", "/v1/jobs/search", json=self._payload(payload))

    def search_companies(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """POST /v1/companies/search — entreprises filtrées par firmographie,
        technologies (`company_technology_slug_or`) et signaux de recrutement
        (`job_filters` + `min_num_jobs_found`).

        Renvoie `{metadata, data: [company…]}` — chaque entreprise porte `name`,
        `domain`, `employee_count`, `industry`, `country_code`, `technology_names`,
        `technology_slugs`, `num_jobs`, `jobs_found`, `technologies_found`,
        `linkedin_url`, `annual_revenue_usd`…

        Coût : 3 crédits API par entreprise rendue → borner `limit`. Une entreprise
        absente de la base (couverture partielle des PME) rend `data: []` — ce n'est
        pas une erreur.
        """
        return self._request("POST", "/v1/companies/search", json=self._payload(payload))

    # --- compte -------------------------------------------------------------

    def credit_balance(self) -> Dict[str, Any]:
        """GET /v0/billing/credit-balance — solde de crédits de l'équipe. Appel
        authentifié GRATUIT : c'est la sonde « la clé marche-t-elle ? » sans dépenser."""
        return self._request("GET", "/v0/billing/credit-balance")
