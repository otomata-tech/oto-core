"""
Apollo.io API Client for lead enrichment and search.

Requires: requests
"""

import time
from typing import Optional, Dict, Any, List

import requests

from ...config import require_secret


class ApolloError(RuntimeError):
    """Erreur API Apollo, **message amont remonté tel quel**.

    `raise_for_status()` nu ne donne que « 422 Client Error … <url> » : l'appelant
    (un agent) ne sait pas QUEL champ est refusé, donc ne peut pas corriger son
    appel. Apollo, lui, dit précisément ce qui cloche dans le corps de la réponse
    (`error`/`errors`/`error_message`) — on le propage.
    """

    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


class ApolloClient:
    """
    Apollo.io API client for:
    - Organization search and enrichment
    - People search and matching
    - Job postings lookup
    """

    # Chemin canonique documenté (`/api/v1`) — `/v1` est un alias legacy qui
    # répond sur enrich/match mais PAS sur les endpoints de recherche.
    BASE_URL = "https://api.apollo.io/api/v1"

    def __init__(self, api_key: str = None):
        """
        Initialize Apollo client.

        Args:
            api_key: Apollo API key (or set APOLLO_API_KEY env var)
        """
        self.api_key = api_key or require_secret("APOLLO_API_KEY")
        self._last_request = 0.0

    def _rate_limit(self):
        """Enforce minimum 1 second between requests."""
        elapsed = time.time() - self._last_request
        if elapsed < 1.0:
            time.sleep(1.0 - elapsed)
        self._last_request = time.time()

    @staticmethod
    def _upstream_message(response: requests.Response) -> str:
        """Message d'erreur d'Apollo, sinon un extrait du corps brut."""
        try:
            body = response.json()
        except ValueError:
            return (response.text or "").strip()[:400]
        if isinstance(body, dict):
            for k in ("error_message", "error", "message", "errors"):
                v = body.get(k)
                if v:
                    return v if isinstance(v, str) else str(v)
        return str(body)[:400]

    def _request(self, method: str, endpoint: str, **kwargs) -> Dict[str, Any]:
        """Make API request. Une erreur HTTP lève `ApolloError` portant le message
        AMONT (quel champ est refusé) — pas un « 422 Client Error » opaque."""
        self._rate_limit()

        url = f"{self.BASE_URL}/{endpoint}"
        headers = {"X-Api-Key": self.api_key, "Content-Type": "application/json"}

        response = requests.request(method, url, headers=headers, **kwargs)
        if not response.ok:
            raise ApolloError(
                f"Apollo {response.status_code} sur {endpoint} : "
                f"{self._upstream_message(response)}",
                status_code=response.status_code,
            )
        return response.json()

    def search_organizations(
        self,
        name: str = None,
        domain: str = None,
        country: str = None,
        per_page: int = 10,
    ) -> Dict[str, Any]:
        """
        Search for organizations.

        Args:
            name: Company name to search
            domain: Domain to search
            country: Country filter
            per_page: Results per page

        Returns:
            Dict with organizations list

        ⚠️ Noms de champs imposés par l'API (`q_organization_name`,
        `q_organization_domains_list`) : un nom inconnu n'est PAS rejeté, il est
        **ignoré silencieusement** → la réponse est la base entière (~28 M
        d'entreprises, top générique Google/Amazon/…) et passe pour un résultat.
        """
        data = {"per_page": per_page}
        if name:
            data["q_organization_name"] = name
        if domain:
            data["q_organization_domains_list"] = [domain]
        if country:
            data["organization_locations"] = [country]

        return self._request("POST", "mixed_companies/search", json=data)

    def enrich_organization(self, domain: str) -> Dict[str, Any]:
        """
        Enrich organization by domain.

        Args:
            domain: Company domain

        Returns:
            Detailed company data
        """
        return self._request("GET", "organizations/enrich", params={"domain": domain})

    def search_people(
        self,
        domains: List[str] = None,
        org_ids: List[str] = None,
        titles: List[str] = None,
        seniorities: List[str] = None,
        per_page: int = 25,
        page: int = 1,
    ) -> Dict[str, Any]:
        """
        Search for people (net-new prospecting).

        Args:
            domains: Company domains to search
            org_ids: Apollo organization IDs
            titles: Title keywords
            seniorities: Seniority levels (e.g., ["c_suite", "director"])
            per_page: Results per page
            page: Page number

        Returns:
            People search results (no email/phone — that's `match_person`)

        ⚠️ L'endpoint est `mixed_people/api_search` et le filtre domaine
        s'appelle `q_organization_domains_list` : `people/search` +
        `organization_domains` rendaient un 422 systématique. Il n'existe PAS de
        filtre « department » sur cette API — cibler par `titles`/`seniorities`.
        """
        data = {"per_page": per_page, "page": page}
        if domains:
            data["q_organization_domains_list"] = domains
        if org_ids:
            data["organization_ids"] = org_ids
        if titles:
            data["person_titles"] = titles
        if seniorities:
            data["person_seniorities"] = seniorities

        return self._request("POST", "mixed_people/api_search", json=data)

    def match_person(
        self,
        linkedin_url: str = None,
        email: str = None,
        first_name: str = None,
        last_name: str = None,
        name: str = None,
        domain: str = None,
        org_name: str = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Match a specific person.

        Args:
            linkedin_url: LinkedIn profile URL
            email: Email address
            first_name: First name
            last_name: Last name
            name: Full name
            domain: Company domain
            org_name: Organization name

        Returns:
            Matched person data or None
        """
        data = {}
        if linkedin_url:
            data["linkedin_url"] = linkedin_url
        if email:
            data["email"] = email
        if first_name:
            data["first_name"] = first_name
        if last_name:
            data["last_name"] = last_name
        if name:
            data["name"] = name
        if domain:
            data["organization_domain"] = domain
        if org_name:
            data["organization_name"] = org_name

        try:
            return self._request("POST", "people/match", json=data)
        except ApolloError as e:
            if e.status_code == 404:
                return None
            raise

    def get_job_postings(self, org_id: str) -> Dict[str, Any]:
        """
        Get job postings for an organization.

        Args:
            org_id: Apollo organization ID

        Returns:
            Job postings list
        """
        return self._request("GET", f"organizations/{org_id}/job_postings")
