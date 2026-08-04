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
        page: int = 1,
        employee_ranges: List[str] = None,
        revenue_min: int = None,
        revenue_max: int = None,
        locations: List[str] = None,
        keywords: List[str] = None,
        technologies: List[str] = None,
        org_ids: List[str] = None,
    ) -> Dict[str, Any]:
        """
        Search for organizations (firmographics en lot).

        Args:
            name: Company name to search
            domain: Domain to search
            country: Country filter (raccourci de `locations`)
            per_page: Results per page (≤100)
            page: Page number
            employee_ranges: Tranches d'effectif, bornes INCLUSES au format
                "min,max" — ex. ["1,10", "11,50"]. LE filtre de qualification par
                taille.
            revenue_min / revenue_max: bornes de chiffre d'affaires annuel
            locations: villes/régions/pays du SIÈGE
            keywords: mots-clés d'activité (`q_organization_keyword_tags`)
            technologies: uids de technologies utilisées (ex. "salesforce")
            org_ids: ids Apollo d'organisations

        Returns:
            Dict with organizations list

        ⚠️ La réponse ne porte PAS `estimated_num_employees` (vérifié) — seulement
        le CA et les taux de croissance d'effectif. Pour l'effectif exact et sa
        répartition par département : `enrich_organization` / `bulk_enrich_organizations`.
        D'où l'intérêt de `employee_ranges` : on FILTRE par taille sans payer un
        enrichissement par entreprise (coût Apollo : 1 crédit la PAGE de 100 ici,
        contre 1 crédit l'ENTREPRISE en enrichissement).

        ⚠️ Noms de champs imposés par l'API (`q_organization_name`,
        `q_organization_domains_list`) : un nom inconnu n'est PAS rejeté, il est
        **ignoré silencieusement** → la réponse est la base entière (~28 M
        d'entreprises, top générique Google/Amazon/…) et passe pour un résultat.
        """
        data: Dict[str, Any] = {"per_page": per_page, "page": page}
        if name:
            data["q_organization_name"] = name
        if domain:
            data["q_organization_domains_list"] = [domain]
        locs = list(locations or []) + ([country] if country else [])
        if locs:
            data["organization_locations"] = locs
        if employee_ranges:
            data["organization_num_employees_ranges"] = employee_ranges
        if revenue_min is not None or revenue_max is not None:
            rng = {}
            if revenue_min is not None:
                rng["min"] = revenue_min
            if revenue_max is not None:
                rng["max"] = revenue_max
            data["revenue_range"] = rng
        if keywords:
            data["q_organization_keyword_tags"] = keywords
        if technologies:
            data["currently_using_any_of_technology_uids"] = technologies
        if org_ids:
            data["organization_ids"] = org_ids

        return self._request("POST", "mixed_companies/search", json=data)

    #: Plafond imposé par l'API sur `organizations/bulk_enrich`.
    BULK_ENRICH_MAX = 10

    def bulk_enrich_organizations(self, domains: List[str]) -> Dict[str, Any]:
        """
        Enrichit jusqu'à 10 entreprises en UN appel (firmographics complètes :
        `estimated_num_employees`, `departmental_head_count`, croissance, CA…).

        Args:
            domains: domaines des entreprises (≤10 — plafond de l'API)

        ⚠️ Le lot n'économise PAS de crédits (1 crédit par organisation, comme en
        unitaire) : il économise des APPELS — le rate limit d'`organizations/enrich`
        est de 600/h, donc ÷10 sur une campagne.
        """
        doms = [d.strip() for d in (domains or []) if d and d.strip()]
        if not doms:
            raise ValueError("domains requis (au moins un domaine)")
        if len(doms) > self.BULK_ENRICH_MAX:
            raise ValueError(
                f"{len(doms)} domaines : l'API en accepte {self.BULK_ENRICH_MAX} "
                "au maximum par appel — découpe en lots")
        return self._request("POST", "organizations/bulk_enrich",
                             params={"domains[]": doms})

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

    @staticmethod
    def _looks_like_stub(person: Optional[Dict[str, Any]]) -> bool:
        """La fiche rendue est-elle un STUB créé faute de match ?

        Sur un identifiant trop faible, Apollo ne renvoie pas « rien » : il CRÉE une
        personne neuve, vide (`last_name`/`title`/`email`/`linkedin_url` à null) et la
        marque `revealed_for_current_team` — le crédit est consommé, la donnée n'existe
        pas. Sans ce test, l'appelant croit avoir enrichi.
        """
        if not isinstance(person, dict):
            return False
        return not any(person.get(k) for k in
                       ("last_name", "title", "email", "linkedin_url", "organization_id"))

    def match_person(
        self,
        person_id: str = None,
        linkedin_url: str = None,
        email: str = None,
        first_name: str = None,
        last_name: str = None,
        name: str = None,
        domain: str = None,
        org_name: str = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Match a specific person (enrichment — 1 crédit Apollo par appel).

        Args:
            person_id: **id Apollo** de la personne (celui que rend `search_people`)
                — l'identifiant le plus sûr, à préférer dès qu'on vient d'un search
            linkedin_url: LinkedIn profile URL
            email: Email address
            first_name: First name
            last_name: Last name
            name: Full name
            domain: Company domain
            org_name: Organization name

        Returns:
            Matched person data, ou None (404). La fiche porte `_stub: True` quand
            Apollo a fabriqué une coquille vide au lieu de matcher (cf. `_looks_like_stub`).

        ⚠️ **Un identifiant faible coûte un crédit pour rien.** `search_people` rend les
        noms de famille OBFUSQUÉS (« Vi***l ») : matcher avec `first_name` + société
        seuls ne retrouve pas la personne, Apollo crée un stub et facture quand même
        (~12 crédits perdus en une session, feedbacks #347-350). D'où la garde
        ci-dessous : sans identifiant fort (`person_id`/`email`/`linkedin_url`), un nom
        COMPLET est exigé — l'appel est refusé AVANT de brûler le crédit.
        """
        strong = person_id or email or linkedin_url
        full_name = bool(last_name) or bool(name and len(name.split()) >= 2)
        if not strong and not full_name:
            raise ValueError(
                "identifiant trop faible pour un match Apollo : passe `person_id` "
                "(l'id rendu par search_people), `email` ou `linkedin_url` — sinon un "
                "nom COMPLET (prénom + nom). Un prénom + une société ne matchent pas : "
                "Apollo crée une fiche vide et consomme quand même le crédit.")

        data = {}
        if person_id:
            data["id"] = person_id
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
            # `domain` est le nom attendu par l'API — `organization_domain` (utilisé
            # jusqu'au 2026-08-04) est un champ INCONNU, donc ignoré en silence : le
            # domaine ne participait pas au match, ce qui rendait les stubs plus probables.
            data["domain"] = domain
        if org_name:
            data["organization_name"] = org_name

        try:
            out = self._request("POST", "people/match", json=data)
        except ApolloError as e:
            if e.status_code == 404:
                return None
            raise
        person = (out or {}).get("person") if isinstance(out, dict) else None
        if self._looks_like_stub(person):
            person["_stub"] = True
        return out

    def get_job_postings(self, org_id: str) -> Dict[str, Any]:
        """
        Get job postings for an organization.

        Args:
            org_id: Apollo organization ID

        Returns:
            Job postings list
        """
        return self._request("GET", f"organizations/{org_id}/job_postings")
