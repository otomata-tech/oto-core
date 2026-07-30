"""Spott ATS/CRM API client (agences de recrutement).

Auth = **clé API** passée en header `x-api-key` (Spott : Settings → API Keys).
Base URL `https://api.gospott.com` (déclarée par la spec OpenAPI officielle).

Vocabulaire Spott — deux mots à ne pas confondre :
- une **vacancy** = un **job** (poste ouvert). L'API garde `/vacancies` dans ses
  chemins, ses libellés disent « job » : on expose « job ».
- un **client** = l'entreprise cliente du cabinet (avec ses **client contacts**,
  les interlocuteurs). Un **candidate** postule via une **application**, qui vit
  dans un **stage** de pipeline. Une **placement** = un placement conclu.

Deux régimes de pagination cohabitent, et on garde ce fait visible :
- les `list_*` (GET) paginent par **curseur** (`limit` ≤ 50, `cursor` renvoyé
  dans la réponse précédente) ;
- les `search_*` (POST `_search`) paginent par **page** (`page`/`pageSize`) et
  prennent un tableau de **filtres structurés** (`type`/`operator`/`path`/`value`)
  — passés bruts, l'agent compose ce dont il a besoin.

Docs : https://api-docs.spott.io

Requires: requests
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import requests

from ...config import require_secret
from ..common import FieldFilter, raise_for_upstream

# Entités qui ont un pipeline de stages (`GET /pipeline/<entity>/stages`).
# `applications`/`vacancies` = les deux pipelines du quotidien recrutement ;
# `clients`/`opportunities` = le côté CRM (développement commercial du cabinet).
PIPELINE_ENTITIES = ("applications", "vacancies", "clients", "opportunities")

# Entités auxquelles une note peut être rattachée (`links[].entityType`).
NOTE_ENTITY_TYPES = ("candidate", "vacancy", "client", "application",
                     "clientContact", "interview", "opportunity")


class SpottClient:
    """Client Spott — candidats, jobs, candidatures, notes, clients, placements."""

    BASE_URL = "https://api.gospott.com"

    def __init__(self, api_key: Optional[str] = None,
                 field_filter: Optional[FieldFilter] = None):
        """Initialise le client.

        Args:
            api_key: clé API Spott (ou env `SPOTT_API_KEY`).
            field_filter: redaction de champs (défaut = politique `spott`) — les
                réponses portent de la PII candidat (emails, téléphones, salaires).
        """
        self.api_key = api_key or require_secret("SPOTT_API_KEY")
        self.field_filter = field_filter or FieldFilter.from_config("spott")
        self.session = requests.Session()
        self.session.headers.update({
            "x-api-key": self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        })

    # --- transport ----------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs) -> Any:
        resp = self.session.request(
            method, f"{self.BASE_URL}{path}", timeout=30, **kwargs)
        raise_for_upstream(resp, service="spott")
        if not resp.content:
            return {}
        return self.field_filter.apply(resp.json())

    @staticmethod
    def _clean(params: Dict[str, Any]) -> Dict[str, Any]:
        """Retire les paramètres non renseignés (l'API rejette un `null` explicite).

        Note : `include` est déclaré `required` dans la spec alors qu'il porte un
        défaut `[]` (artefact zod→OpenAPI : un champ à `.default()` reste optionnel
        en entrée) — on l'omet donc quand l'appelant n'en demande pas.
        """
        return {k: v for k, v in params.items() if v not in (None, [], ())}

    @staticmethod
    def _page(page: Optional[int], page_size: Optional[int],
              filters: Optional[List[dict]]) -> Dict[str, Any]:
        """Corps commun des endpoints `_search` (filtres + pagination par page)."""
        body: Dict[str, Any] = {"filters": filters or []}
        if page is not None:
            body["page"] = page
        if page_size is not None:
            body["pageSize"] = page_size
        return body

    # --- Candidats ----------------------------------------------------------

    def list_candidates(
        self,
        limit: int = 25,
        cursor: Optional[str] = None,
        modified_since: Optional[str] = None,
        modified_until: Optional[str] = None,
        list_ids: Optional[List[str]] = None,
        include: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Liste les candidats (curseur, `limit` ≤ 50).

        Args:
            modified_since / modified_until: bornes ISO-8601 sur la modification.
            list_ids: restreint à des listes Spott (≤ 25).
            include: relations à embarquer — `skills`.
        """
        return self._request("GET", "/candidates", params=self._clean({
            "limit": min(limit, 50), "cursor": cursor,
            "modifiedSince": modified_since, "modifiedUntil": modified_until,
            "listIds": list_ids, "include": include,
        }))

    def get_candidate(self, candidate_id: str) -> Dict[str, Any]:
        """Récupère un candidat (identité, contacts, contacts clients liés)."""
        return self._request("GET", f"/candidates/{candidate_id}")

    def search_candidates(
        self,
        filters: Optional[List[dict]] = None,
        page: Optional[int] = None,
        page_size: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Cherche des candidats par filtres structurés (pagination par page).

        Args:
            filters: liste de filtres `{type, operator, path, value}`. Champs
                natifs : `candidate.firstName` / `candidate.lastName` (type
                `text`, opérateurs contains|equals|startsWith|notEquals),
                `candidate.mainContact` (`entitySelect`, in|notIn),
                `candidate.createdAt` (`date`). Les attributs personnalisés
                passent par les types `custom*` (cf. doc Spott).
        """
        return self._request("POST", "/candidates/_search",
                             json=self._page(page, page_size, filters))

    def create_candidate(self, candidate: Dict[str, Any]) -> Dict[str, Any]:
        """Crée un candidat.

        Args:
            candidate: objet candidat — `firstName` et `lastName` obligatoires ;
                puis `emails` / `phoneNumbers` (`{email|phoneNumber, purpose,
                isPrimary}`), `locations`, `socialMedia` (`{url, type}` avec type
                LINKEDIN|TWITTER|FACEBOOK|INSTAGRAM), `education`,
                `workExperiences`, `certifications`, `languages`, `skills`,
                `compensation`, `status`, `customAttributes`…
        """
        return self._request("POST", "/candidates", json=candidate)

    def update_candidate(self, candidate_id: str,
                         patch: Dict[str, Any]) -> Dict[str, Any]:
        """Met à jour un candidat (PATCH partiel : seuls les champs fournis)."""
        return self._request("PATCH", f"/candidates/{candidate_id}", json=patch)

    # --- Jobs (vacancies) ---------------------------------------------------

    def list_jobs(
        self,
        limit: int = 25,
        cursor: Optional[str] = None,
        company_ids: Optional[List[str]] = None,
        candidate_emails: Optional[List[str]] = None,
        modified_since: Optional[str] = None,
        modified_until: Optional[str] = None,
        include: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Liste les jobs (postes ; endpoint `/vacancies`, curseur, `limit` ≤ 50).

        Args:
            company_ids: restreint aux jobs de ces entreprises clientes.
            candidate_emails: jobs où ces candidats (≤ 25 emails) ont postulé.
            include: relations à embarquer — `jobBoards`.
        """
        return self._request("GET", "/vacancies", params=self._clean({
            "limit": min(limit, 50), "cursor": cursor,
            "companyIds": company_ids, "candidateEmailAddresses": candidate_emails,
            "modifiedSince": modified_since, "modifiedUntil": modified_until,
            "include": include,
        }))

    def get_job(self, job_id: str) -> Dict[str, Any]:
        """Récupère un job (détail, attributs personnalisés, métadonnées)."""
        return self._request("GET", f"/vacancies/{job_id}")

    def search_jobs(
        self,
        filters: Optional[List[dict]] = None,
        page: Optional[int] = None,
        page_size: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Cherche des jobs par filtres structurés (pagination par page).

        Args:
            filters: champs natifs `vacancy.name` / `vacancy.client.company.name`
                (`text`), `vacancy.client.company` / `vacancy.team` /
                `vacancy.stage` (`entitySelect`, in|notIn),
                `vacancy.stage.isOpen` (`boolean`) — « les postes ouverts ».
        """
        return self._request("POST", "/vacancies/_search",
                             json=self._page(page, page_size, filters))

    # --- Candidatures (applications) ----------------------------------------

    def list_applications(
        self,
        limit: int = 25,
        cursor: Optional[str] = None,
        job_ids: Optional[List[str]] = None,
        candidate_emails: Optional[List[str]] = None,
        is_inbound: Optional[bool] = None,
        modified_since: Optional[str] = None,
        modified_until: Optional[str] = None,
        include: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Liste les candidatures (curseur, `limit` ≤ 50).

        Args:
            job_ids: restreint à ces jobs (`vacancyIds` côté API).
            candidate_emails: ≤ 25 emails de candidats.
            is_inbound: True = candidatures spontanées/entrantes seulement.
            include: `lastActivity`, `candidate.latestWorkExperience`,
                `candidate.locations`, `candidate.emailAddresses`,
                `candidate.phoneNumbers`, `vacancy.clientContactTeam`,
                `vacancy.jobBoards`.
        """
        return self._request("GET", "/applications", params=self._clean({
            "limit": min(limit, 50), "cursor": cursor,
            "vacancyIds": job_ids, "candidateEmailAddresses": candidate_emails,
            "isInbound": is_inbound,
            "modifiedSince": modified_since, "modifiedUntil": modified_until,
            "include": include,
        }))

    def get_application(self, application_id: str,
                        include: Optional[List[str]] = None) -> Dict[str, Any]:
        """Récupère une candidature."""
        return self._request("GET", f"/applications/{application_id}",
                             params=self._clean({"include": include}))

    def applications_by_candidate(self, candidate_id: str) -> Dict[str, Any]:
        """Candidatures d'un candidat (jobs + spontanées vers un client),
        de l'activité la plus récente à la plus ancienne."""
        return self._request("GET", f"/applications/candidate/{candidate_id}")

    def applications_by_job(self, job_id: str) -> Dict[str, Any]:
        """Candidatures d'un job (candidat, statut, avancement dans le pipeline)."""
        return self._request("GET", f"/applications/vacancy/{job_id}")

    def create_application(
        self,
        candidate_id: str,
        stage_id: str,
        job_id: Optional[str] = None,
        status_id: Optional[str] = None,
        client_id: Optional[str] = None,
        **extra: Any,
    ) -> Dict[str, Any]:
        """Fait postuler un candidat — à un job, ou à un client (spontanée).

        Args:
            stage_id: étape de pipeline de départ (cf. `pipeline_stages`).
            job_id: le job visé ; `None` + `client_id` = candidature spontanée.
            status_id: statut dans l'étape (optionnel).
            **extra: champs bruts de l'API (`teamUserIds`, `clientTeamContactIds`,
                `owner`, `position`).
        """
        body: Dict[str, Any] = {
            "candidateId": candidate_id, "stageId": stage_id,
            "vacancyId": job_id, "statusId": status_id,
        }
        if client_id:
            body["clientId"] = client_id
        body.update(extra)
        return self._request("POST", "/applications", json=body)

    def move_application(self, application_id: str, stage_id: str,
                         status_id: Optional[str] = None) -> Dict[str, Any]:
        """Déplace une candidature vers une autre étape du pipeline du job."""
        body: Dict[str, Any] = {"stageId": stage_id}
        if status_id is not None:
            body["statusId"] = status_id
        return self._request("PUT", f"/applications/{application_id}/move",
                             json=body)

    def application_activities(self, application_id: str) -> Dict[str, Any]:
        """Journal d'activité d'une candidature (changements d'étape, actions)."""
        return self._request("GET", f"/applications/{application_id}/activities")

    def pipeline_stages(self, entity: str = "applications",
                        template_id: Optional[str] = None) -> Dict[str, Any]:
        """Étapes ordonnées d'un pipeline.

        Args:
            entity: applications | vacancies | clients | opportunities.
            template_id: pipeline d'un template précis (applications seulement).
        """
        if entity not in PIPELINE_ENTITIES:
            raise ValueError(
                f"pipeline Spott inconnu : {entity!r} — attendu "
                f"{', '.join(PIPELINE_ENTITIES)}")
        return self._request("GET", f"/pipeline/{entity}/stages",
                             params=self._clean({"templateId": template_id}))

    # --- Notes --------------------------------------------------------------

    def list_notes(
        self,
        limit: int = 25,
        cursor: Optional[str] = None,
        candidate_id: Optional[str] = None,
        client_contact_id: Optional[str] = None,
        source: Optional[str] = None,
        label_ids: Optional[List[str]] = None,
        modified_since: Optional[str] = None,
        modified_until: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Liste les notes (curseur, `limit` ≤ 50).

        Args:
            source: phone | phoneInbound | phoneOutbound | inPerson |
                onlineMeeting | callAttempted.
        """
        return self._request("GET", "/notes", params=self._clean({
            "limit": min(limit, 50), "cursor": cursor,
            "candidateId": candidate_id, "clientContactId": client_contact_id,
            "source": source, "labelIds": label_ids,
            "modifiedSince": modified_since, "modifiedUntil": modified_until,
        }))

    def create_note(
        self,
        content: str,
        title: Optional[str] = None,
        links: Optional[List[dict]] = None,
        source: Optional[str] = None,
        label_ids: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Crée une note, éventuellement rattachée à des enregistrements.

        Args:
            links: `[{"entityType": …, "entityId": …}]` — entityType parmi
                candidate, vacancy, client, application, clientContact,
                interview, opportunity.
            source: canal de l'échange (cf. `list_notes`).
        """
        for link in links or []:
            kind = link.get("entityType")
            if kind not in NOTE_ENTITY_TYPES:
                raise ValueError(
                    f"entityType Spott inconnu : {kind!r} — attendu "
                    f"{', '.join(NOTE_ENTITY_TYPES)}")
        body: Dict[str, Any] = {"title": title, "content": content}
        if links:
            body["links"] = links
        if source:
            body["source"] = source
        if label_ids:
            body["labelIds"] = label_ids
        return self._request("POST", "/notes", json=body)

    # --- Clients (entreprises clientes du cabinet) --------------------------

    def list_clients(
        self,
        limit: int = 25,
        cursor: Optional[str] = None,
        list_ids: Optional[List[str]] = None,
        modified_since: Optional[str] = None,
        modified_until: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Liste les clients (entreprises ; curseur, `limit` ≤ 50)."""
        return self._request("GET", "/clients", params=self._clean({
            "limit": min(limit, 50), "cursor": cursor, "listIds": list_ids,
            "modifiedSince": modified_since, "modifiedUntil": modified_until,
        }))

    def get_client(self, client_id: str) -> Dict[str, Any]:
        """Récupère un client (société, contacts, secteur, taille, hiérarchies)."""
        return self._request("GET", f"/clients/{client_id}")

    def search_clients(
        self,
        filters: Optional[List[dict]] = None,
        page: Optional[int] = None,
        page_size: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Cherche des clients par filtres structurés (pagination par page).

        Args:
            filters: champs natifs `client.company.name` / `.domain` /
                `.description` (`text`), `client.stage` / `client.contacts`
                (`entitySelect`).
        """
        return self._request("POST", "/clients/_search",
                             json=self._page(page, page_size, filters))

    def list_client_contacts(
        self,
        limit: int = 25,
        cursor: Optional[str] = None,
        client_ids: Optional[List[str]] = None,
        list_ids: Optional[List[str]] = None,
        modified_since: Optional[str] = None,
        modified_until: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Liste les contacts clients (interlocuteurs ; curseur, `limit` ≤ 50)."""
        return self._request("GET", "/clients/contacts", params=self._clean({
            "limit": min(limit, 50), "cursor": cursor,
            "client_ids": client_ids, "listIds": list_ids,
            "modifiedSince": modified_since, "modifiedUntil": modified_until,
        }))

    # --- Placements ---------------------------------------------------------

    def list_placements(
        self,
        page: int = 0,
        page_size: int = 20,
        company_id: Optional[str] = None,
        modified_since: Optional[str] = None,
        modified_until: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Liste les placements (candidat, société, job, honoraires).

        ⚠️ Pagination par **page** ici (pas de curseur) : `page` (0-based),
        `pageSize` ≤ 100.
        """
        return self._request("GET", "/placements", params=self._clean({
            "page": page, "pageSize": min(page_size, 100),
            "companyId": company_id,
            "modifiedSince": modified_since, "modifiedUntil": modified_until,
        }))

    # --- Transverse ---------------------------------------------------------

    def search_people(self, query: str, limit: int = 25) -> Dict[str, Any]:
        """Cherche une personne (candidats ∪ contacts clients) par nom, email ou
        téléphone — matching flou, classé par pertinence. `limit` ≤ 100."""
        return self._request("GET", "/search/people", params={
            "query": query, "limit": min(limit, 100)})

    def list_users(self, include_deactivated: bool = False) -> Dict[str, Any]:
        """Liste les utilisateurs Spott (recruteurs). Sert aussi de sonde de
        connexion : le plus petit appel authentifié de l'API."""
        return self._request("GET", "/users", params=self._clean({
            "includeDeactivated": include_deactivated or None}))
