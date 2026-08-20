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
        person_locations: List[str] = None,
        organization_locations: List[str] = None,
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
            person_locations: Where the PERSON is, e.g. ["France", "Paris, France"]
            organization_locations: Where their EMPLOYER's site is
            per_page: Results per page
            page: Page number

        Returns:
            People search results (no email/phone — that's `match_person`)

        ⚠️ L'endpoint est `mixed_people/api_search` et le filtre domaine
        s'appelle `q_organization_domains_list` : `people/search` +
        `organization_domains` rendaient un 422 systématique. Il n'existe PAS de
        filtre « department » sur cette API — cibler par `titles`/`seniorities`.

        La LOCALISATION est ce qui rend le domaine exploitable sur un groupe
        mondial : `franke.com` rend 1887 profils, `verifone.com` 3282, tous pays
        confondus, et rien d'autre ne permet d'en isoler la filiale française —
        chaque reveal à l'aveugle coûtant un crédit.
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
        if person_locations:
            data["person_locations"] = person_locations
        if organization_locations:
            data["organization_locations"] = organization_locations

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

    # ------------------------------------------------------------------
    # Email accounts & schedules (prérequis en lecture des séquences/emails :
    # sans un `id` d'ici, `create_sequence`/`add_contacts_to_sequence` n'ont
    # rien à passer en `emailer_schedule_id`/`send_email_from_email_account_id`)
    # ------------------------------------------------------------------

    def list_email_accounts(self) -> Dict[str, Any]:
        """
        List the mailboxes connected to this Apollo account (0 crédit).

        Returns:
            Dict avec `email_accounts` — c'est ici qu'on trouve l'`id` à passer
            en `send_email_from_email_account_id` à `add_contacts_to_sequence`.
        """
        return self._request("GET", "email_accounts")

    def list_email_schedules(self) -> Dict[str, Any]:
        """
        List the send schedules configured on this team (0 crédit).

        Returns:
            Dict avec `emailer_schedules` — c'est ici qu'on trouve l'`id` à
            passer en `emailer_schedule_id` à `create_sequence` (requis, sans
            lui la création échoue).
        """
        return self._request("GET", "emailer_schedules")

    # ------------------------------------------------------------------
    # Sequences
    #
    # ⚠️ Deux familles de chemin, PAS une incohérence : create/update utilisent
    # `/sequences[...]` (REST plus récent), tout le reste — search, contacts,
    # activate/deactivate/archive — reste sur l'objet legacy `/emailer_campaigns`.
    # Vérifié endpoint par endpoint dans la doc Apollo (2026-08-20) ; pas encore
    # rejoué en vrai (pas de clé dans cet environnement) — à confirmer au premier
    # run réel plutôt qu'à supposer une symétrie qui n'existe pas.
    # ------------------------------------------------------------------

    def search_sequences(
        self,
        name: str = None,
        per_page: int = 25,
        page: int = 1,
    ) -> Dict[str, Any]:
        """
        Search sequences by name (0 crédit).

        Args:
            name: mots-clés, doit matcher une PARTIE du nom (`q_name` côté API)
            per_page: résultats par page
            page: numéro de page

        Returns:
            Dict avec `emailer_campaigns` (liste) et `pagination`
        """
        data: Dict[str, Any] = {"per_page": per_page, "page": page}
        if name:
            data["q_name"] = name
        return self._request("POST", "emailer_campaigns/search", json=data)

    def create_sequence(
        self,
        name: str,
        emailer_schedule_id: str,
        active: bool = False,
        label_names: List[str] = None,
        folder_id: str = None,
        max_emails_per_day: int = None,
        emailer_steps: List[Dict[str, Any]] = None,
        **extra: Any,
    ) -> Dict[str, Any]:
        """
        Create a sequence (0 crédit — le coût est dans l'envoi, pas la création).

        Args:
            name: nom de la séquence
            emailer_schedule_id: id d'un planning d'envoi — REQUIS par l'API,
                obtenu via `list_email_schedules` (aucun défaut implicite documenté)
            active: activer immédiatement (défaut False — préférer `activate_sequence`
                une fois les étapes/templates relus)
            label_names: labels à appliquer (créés si absents)
            folder_id: id de dossier Apollo
            max_emails_per_day: plafond d'envoi quotidien
            emailer_steps: définition des étapes (voir doc Apollo — structure
                imbriquée, non validée localement)
            **extra: autres champs documentés (`sequence_by_exact_daytime`,
                `mark_finished_if_reply`, `mark_paused_if_ooo`, etc.) passés tels quels

        Returns:
            Dict avec `emailer_campaign`, `emailer_steps`, `emailer_touches`, `emailer_templates`
        """
        if not (name or "").strip():
            raise ValueError("name requis pour créer une séquence")
        if not (emailer_schedule_id or "").strip():
            raise ValueError(
                "emailer_schedule_id requis — obtiens-le via list_email_schedules(), "
                "l'API refuse la création sans planning d'envoi")
        data: Dict[str, Any] = {
            "name": name,
            "emailer_schedule_id": emailer_schedule_id,
            "active": active,
        }
        if label_names:
            data["label_names"] = label_names
        if folder_id:
            data["folder_id"] = folder_id
        if max_emails_per_day is not None:
            data["max_emails_per_day"] = max_emails_per_day
        if emailer_steps:
            data["emailer_steps"] = emailer_steps
        data.update(extra)
        return self._request("POST", "sequences", json=data)

    def update_sequence(self, sequence_id: str, **fields: Any) -> Dict[str, Any]:
        """
        Update a sequence (0 crédit). Tous les champs sont optionnels côté API —
        seuls ceux passés ici sont envoyés.

        Args:
            sequence_id: id Apollo de la séquence
            **fields: `name`, `active`, `emailer_schedule_id`, `label_names`,
                `max_emails_per_day`, `cc_emails`, `bcc_emails`, `emailer_steps`
                (inclure `id` par step pour MODIFIER, l'omettre pour EN CRÉER une),
                `sharing_permission`, etc. — voir doc Apollo

        Returns:
            Séquence mise à jour
        """
        if not (sequence_id or "").strip():
            raise ValueError("sequence_id requis")
        return self._request("PUT", f"sequences/{sequence_id}", json=fields)

    def activate_sequence(self, sequence_id: str) -> Dict[str, Any]:
        """
        Activate a sequence — démarre l'envoi programmé (0 crédit à l'appel,
        les crédits sont consommés au fil des envois/reveals).

        ⚠️ Échoue en 422 si la séquence est déjà active ou n'a pas d'étape.
        """
        if not (sequence_id or "").strip():
            raise ValueError("sequence_id requis")
        return self._request("POST", f"emailer_campaigns/{sequence_id}/approve")

    def deactivate_sequence(self, sequence_id: str) -> Dict[str, Any]:
        """Deactivate a sequence (0 crédit). 422 si déjà inactive."""
        if not (sequence_id or "").strip():
            raise ValueError("sequence_id requis")
        return self._request("POST", f"emailer_campaigns/{sequence_id}/abort")

    def archive_sequence(self, sequence_id: str) -> Dict[str, Any]:
        """Archive a sequence (0 crédit). Nécessite d'en être propriétaire ou
        d'avoir un accès partagé « full access »."""
        if not (sequence_id or "").strip():
            raise ValueError("sequence_id requis")
        return self._request("POST", f"emailer_campaigns/{sequence_id}/archive")

    def add_contacts_to_sequence(
        self,
        sequence_id: str,
        send_email_from_email_account_id: str,
        contact_ids: List[str] = None,
        label_names: List[str] = None,
        send_email_from_email_address: str = None,
        status: str = None,
        **flags: Any,
    ) -> Dict[str, Any]:
        """
        Enroll contacts in a sequence — l'appel le plus à risque de ce client :
        il démarre une campagne automatisée MULTI-ÉTAPES vers des personnes réelles,
        pas un envoi unique. 0 crédit à l'appel (les envois/reveals suivants coûtent).

        Args:
            sequence_id: id Apollo de la séquence
            send_email_from_email_account_id: id (ou liste d'ids pour rotation) de
                la boîte CONNECTÉE qui enverra — REQUIS par l'API, obtenu via
                `list_email_accounts`. Verrou local ci-dessous, même logique que
                Lightfield `_check_from` : sans boîte explicite, aucun appel ne part.
            contact_ids: ids Apollo des contacts (mutuellement substituable à `label_names`)
            label_names: labels identifiant les contacts à ajouter
            send_email_from_email_address: adresse précise dans le compte (si multi-alias)
            status: `"active"` ou `"paused"` à l'ajout
            **flags: `sequence_no_email`, `sequence_unverified_email`,
                `sequence_job_change`, `sequence_active_in_other_campaigns`,
                `sequence_finished_in_other_campaigns`,
                `sequence_same_company_in_same_campaign`,
                `contacts_without_ownership_permission`, `add_if_in_queue`,
                `contact_verification_skipped`, `user_id`, `auto_unpause_at` —
                tous des garde-fous Apollo à `False`/absent par défaut ; les passer
                explicitement à `True` pour les lever

        ⚠️ Doc Apollo : 403 « Master API key required » sur cet endpoint — à
        confirmer avec une clé réelle, pas vérifié depuis cet environnement.

        Returns:
            Dict avec `contacts` (ajoutés), `skipped_contact_ids` (id → raison),
            `emailer_campaign`, `emailer_steps`, `emailer_touches`
        """
        if not (sequence_id or "").strip():
            raise ValueError("sequence_id requis")
        if not send_email_from_email_account_id:
            raise ValueError(
                "send_email_from_email_account_id requis — obtiens-le via "
                "list_email_accounts() : sans boîte CONNECTÉE explicite, l'API "
                "refuse d'enrôler les contacts (et sans ce verrou, un appel "
                "partirait sur un défaut qu'on ne contrôle pas)")
        if not contact_ids and not label_names:
            raise ValueError("contact_ids ou label_names requis (au moins un des deux)")
        params: Dict[str, Any] = {
            "emailer_campaign_id": sequence_id,
            "send_email_from_email_account_id": send_email_from_email_account_id,
        }
        if contact_ids:
            params["contact_ids[]"] = contact_ids
        if label_names:
            params["label_names[]"] = label_names
        if send_email_from_email_address:
            params["send_email_from_email_address"] = send_email_from_email_address
        if status:
            params["status"] = status
        params.update(flags)
        # Query params (doc Apollo), PAS un body JSON — un champ envoyé en `json`
        # ici serait ignoré en silence, même défaut que `organization_domain`
        # historique sur `match_person` (cf. commentaire plus haut dans ce fichier).
        return self._request(
            "POST", f"emailer_campaigns/{sequence_id}/add_contact_ids", params=params)

    def update_sequence_contact_status(
        self,
        emailer_campaign_ids: List[str],
        contact_ids: List[str],
        mode: str,
    ) -> Dict[str, Any]:
        """
        Mark-as-finished / remove / stop des contacts dans une ou plusieurs
        séquences (0 crédit).

        Args:
            emailer_campaign_ids: ids Apollo des séquences concernées
            contact_ids: ids Apollo des contacts concernés
            mode: `"mark_as_finished"` (termine), `"remove"` (retire de la
                séquence) ou `"stop"` (arrête la progression)

        Returns:
            Dict avec `entity_progress_job` (job async — pas de statut final ici)
        """
        if not emailer_campaign_ids:
            raise ValueError("emailer_campaign_ids requis")
        if not contact_ids:
            raise ValueError("contact_ids requis")
        if mode not in ("mark_as_finished", "remove", "stop"):
            raise ValueError('mode doit être "mark_as_finished", "remove" ou "stop"')
        params = {
            "emailer_campaign_ids[]": emailer_campaign_ids,
            "contact_ids[]": contact_ids,
            "mode": mode,
        }
        return self._request(
            "POST", "emailer_campaigns/remove_or_stop_contact_ids", params=params)

    def get_contact_sequence_activity(
        self,
        contact_id: str,
        sequence_id: str = None,
        per_page: int = 50,
    ) -> Dict[str, Any]:
        """
        Sequence activity feed for one contact (0 crédit).

        Args:
            contact_id: id Apollo du contact — doit appartenir à ton équipe (404 sinon)
            sequence_id: filtre sur une séquence (toutes si omis)
            per_page: 1-50, les événements les PLUS RÉCENTS (pas une pagination)

        Returns:
            Dict avec `events` (ordre du plus récent au plus ancien, max `per_page`)
        """
        if not (contact_id or "").strip():
            raise ValueError("contact_id requis")
        data: Dict[str, Any] = {"contact_id": contact_id, "per_page": per_page}
        if sequence_id:
            data["sequence_id"] = sequence_id
        return self._request("POST", "emailer_campaigns/activity_feed", json=data)

    # ------------------------------------------------------------------
    # One-off emails
    #
    # ⚠️ Brouillon et envoi restent deux appels distincts (create_email_draft
    # PUIS send_email_now), jamais fusionnés — même choix que Lightfield
    # (`draft_email`/`send_email`) et pour la même raison : que rien ne
    # confonde « préparer » et « envoyer ».
    # ------------------------------------------------------------------

    def create_email_draft(
        self,
        contact_id: str = None,
        subject: str = None,
        body_html: str = None,
        recipients: List[Dict[str, str]] = None,
        in_response_to_emailer_message_id: str = None,
        emailer_template_id: str = None,
        attachment_ids: List[str] = None,
        enable_tracking: bool = None,
        outreach_task_id: str = None,
    ) -> Dict[str, Any]:
        """
        Create an email draft (ne part PAS — utiliser send_email_now pour envoyer).

        Args:
            contact_id: id Apollo du destinataire — requis SAUF si
                `in_response_to_emailer_message_id` est fourni (réponse à un fil)
            subject / body_html: contenu (l'API assainit le HTML)
            recipients: `[{"email":, "contact_id":, "recipient_type_cd": "to"|"cc"|"bcc"}]`
            in_response_to_emailer_message_id: id du message parent (fil de réponse)
            emailer_template_id: template Apollo à associer
            attachment_ids: ids de pièces jointes Apollo (issus d'un cycle d'upload
                hors périmètre de ce client — non fabriqués ici, cf. limite similaire
                documentée sur Lightfield `send_email`)
            enable_tracking: activer le suivi ouverture/clic
            outreach_task_id: tâche Apollo à lier au brouillon

        ⚠️ Aucun champ de boîte d'envoi (`email_account_id`/`from`) n'est documenté
        sur CET endpoint — la boîte se décide à `send_email_now` (implicitement, ou
        via la boîte par défaut du compte). Pas de verrou local équivalent à
        `add_contacts_to_sequence` ici : rien à vérifier avant l'écriture du brouillon.

        Returns:
            Dict avec `emailer_message` (status `"drafted"`), `task` si lié
        """
        if not contact_id and not in_response_to_emailer_message_id:
            raise ValueError(
                "contact_id requis, sauf en réponse à un fil "
                "(in_response_to_emailer_message_id)")
        data: Dict[str, Any] = {}
        if contact_id:
            data["contact_id"] = contact_id
        if subject:
            data["subject"] = subject
        if body_html:
            data["body_html"] = body_html
        if recipients:
            data["recipients"] = recipients
        if in_response_to_emailer_message_id:
            data["in_response_to_emailer_message_id"] = in_response_to_emailer_message_id
        if emailer_template_id:
            data["emailer_template_id"] = emailer_template_id
        if attachment_ids:
            data["attachment_ids"] = attachment_ids
        if enable_tracking is not None:
            data["enable_tracking"] = enable_tracking
        if outreach_task_id:
            data["outreach_task_id"] = outreach_task_id
        return self._request("POST", "emailer_messages", json=data)

    def send_email_now(self, message_id: str, surface: str = None) -> Dict[str, Any]:
        """
        Send an existing draft NOW — le seul geste de ce client qui atteint une
        personne réelle par email direct (hors séquence). Irréversible.

        Args:
            message_id: id rendu par create_email_draft
            surface: attribution interne Apollo (optionnel, ex. "emails")

        Returns:
            Dict avec `emailer_message` (status mis à jour), `task` si lié
        """
        if not (message_id or "").strip():
            raise ValueError("message_id requis")
        data: Dict[str, Any] = {}
        if surface:
            data["surface"] = surface
        return self._request("POST", f"emailer_messages/{message_id}/send_now", json=data)

    def check_email_send_status(self, message_id: str) -> Dict[str, Any]:
        """
        Poll the send status of a message (0 crédit).

        Args:
            message_id: id du message (rendu par create_email_draft/send_email_now)

        Returns:
            Dict avec `status`, et selon l'état : `completed_at`, ou
            `failure_reason`/`not_sent_reason`/`failed_at`, ou `retry_after_seconds`
        """
        if not (message_id or "").strip():
            raise ValueError("message_id requis")
        return self._request("POST", "emailer_messages/email_send_status", json={"id": message_id})

    def search_emails(
        self,
        stats: List[str] = None,
        reply_classes: List[str] = None,
        sequence_ids: List[str] = None,
        exclude_sequence_ids: List[str] = None,
        keywords: str = None,
        date_range_mode: str = None,
        date_min: str = None,
        date_max: str = None,
        per_page: int = 25,
        page: int = 1,
    ) -> Dict[str, Any]:
        """
        Search sent/outreach emails (0 crédit). Plafond amont : 50 000 résultats
        affichables (100/page × 500 pages).

        Args:
            stats: statuts (`delivered`, `scheduled`, `drafted`, `not_opened`,
                `opened`, `clicked`, `unsubscribed`, `demoed`, `bounced`,
                `spam_blocked`, `failed_other`)
            reply_classes: sentiment de la réponse (`willing_to_meet`,
                `follow_up_question`, `person_referral`, `out_of_office`,
                `already_left_company_or_not_right_person`, `not_interested`,
                `unsubscribe`, `none_of_the_above`)
            sequence_ids / exclude_sequence_ids: inclure/exclure par séquence
            keywords: recherche plein texte (`q_keywords`)
            date_range_mode: `"due_at"` ou `"completed_at"`
            date_min / date_max: bornes `YYYY-MM-DD`
            per_page: ≤100. page: numéro de page

        Returns:
            Dict avec `emailer_messages`, `emailer_steps`
        """
        params: Dict[str, Any] = {"page": page, "per_page": per_page}
        if stats:
            params["emailer_message_stats[]"] = stats
        if reply_classes:
            params["emailer_message_reply_classes[]"] = reply_classes
        if sequence_ids:
            params["emailer_campaign_ids[]"] = sequence_ids
        if exclude_sequence_ids:
            params["not_emailer_campaign_ids[]"] = exclude_sequence_ids
        if keywords:
            params["q_keywords"] = keywords
        if date_range_mode:
            params["emailer_message_date_range_mode"] = date_range_mode
        if date_min:
            params["emailer_message_date_range[min]"] = date_min
        if date_max:
            params["emailer_message_date_range[max]"] = date_max
        return self._request("GET", "emailer_messages/search", params=params)

    def get_email_content(self, ids: List[str], body_format: str = "plain") -> Dict[str, Any]:
        """
        Fetch the body of up to 10 SENT emails (0 crédit).

        Args:
            ids: ids Apollo des emails envoyés — 10 max, le surplus est ignoré
                EN SILENCE côté API (pas d'erreur)
            body_format: `"plain"` (défaut) ou `"html"` — toute autre valeur
                retombe silencieusement sur `"plain"` côté API

        Returns:
            Dict avec `emailer_messages` (dans l'ordre demandé ; ids sans match
            omis en silence — seuls les emails effectivement ENVOYÉS sont rendus)
        """
        if not ids:
            raise ValueError("ids requis (au moins un)")
        data: Dict[str, Any] = {"ids": ids[:10]}
        if body_format:
            data["body_format"] = body_format
        return self._request("POST", "emailer_messages/get_content", json=data)

    def get_email_stats(self, message_id: str) -> Dict[str, Any]:
        """
        Open/click stats for one sent email (0 crédit).

        ⚠️ Doc Apollo : nécessite une clé « Master » et n'est PAS disponible en
        OAuth — à confirmer avec une clé réelle, pas vérifié depuis cet
        environnement (403 sinon).

        Args:
            message_id: id du message — obtenu via search_emails

        Returns:
            Dict avec `emailer_message` (`num_opens`, `num_clicks`, ...), `activities`
        """
        if not (message_id or "").strip():
            raise ValueError("message_id requis")
        return self._request("GET", f"emailer_messages/{message_id}/activities")

    # ------------------------------------------------------------------
    # Conversations (appels/visios enregistrés — coût conditionnel : 1 crédit
    # seulement si la conversation a des insights IA, 0 sinon — imprévisible
    # avant l'appel, donc pas métré ici ; connecteur byo-only côté backend)
    # ------------------------------------------------------------------

    def search_conversations(
        self,
        conversation_type: str = None,
        account_id: str = None,
        contact_ids: List[str] = None,
        tag_ids: List[str] = None,
        tracker_ids: List[str] = None,
        organization_ids: List[str] = None,
        date_range: Dict[str, str] = None,
        per_page: int = 25,
        page: int = 1,
    ) -> Dict[str, Any]:
        """
        Search recorded conversations (0 crédit — le coût est sur get_conversation).

        Args:
            conversation_type: `"video_conference"` ou `"phone_call"`
            account_id: filtre par compte Apollo
            contact_ids / organization_ids / tag_ids / tracker_ids: filtres
            date_range: `{"start": ISO8601, "end": ISO8601}`
            per_page: résultats par page. page: numéro de page

        Returns:
            Dict avec `conversations`, `pagination`
        """
        data: Dict[str, Any] = {"page": page, "num_fetch_result": per_page}
        if conversation_type:
            data["conversation_type"] = conversation_type
        if account_id:
            data["account_id"] = account_id
        if contact_ids:
            data["contact_ids"] = contact_ids
        if tag_ids:
            data["tag_ids"] = tag_ids
        if tracker_ids:
            data["tracker_ids"] = tracker_ids
        if organization_ids:
            data["organization_ids"] = organization_ids
        if date_range:
            data["date_range"] = date_range
        return self._request("POST", "conversations/search", json=data)

    def get_conversation(self, conversation_id: str) -> Dict[str, Any]:
        """
        Get one conversation — transcript, enregistrement, participants.

        ⚠️ 1 crédit Apollo SI la conversation a des insights IA, 0 sinon —
        imprévisible avant l'appel (pas un coût fixe qu'on peut prédire ni métrer
        a priori).

        Args:
            conversation_id: id de conversation — accepte `id_shareid`

        Returns:
            Dict avec `transcript`, `participants`, `video_recording`/`audio_recording`,
            `opportunities`
        """
        if not (conversation_id or "").strip():
            raise ValueError("conversation_id requis")
        return self._request("GET", f"conversations/{conversation_id}")

    def export_conversations(self, start_time: str, end_time: str, email: str) -> Dict[str, Any]:
        """
        Kick off an async export of conversations over a time range.

        N'attend PAS la fin de l'export — rend un `export_id` à repasser à
        `get_conversations_export` pour poller (asynchrone côté Apollo ; ne pas
        bloquer dessus côté appelant — un export peut prendre largement plus que
        le timeout d'invocation d'un outil).

        Args:
            start_time / end_time: bornes ISO 8601 en GMT, `start_time` < `end_time`
            email: adresse d'un membre de l'équipe à notifier quand l'export est prêt

        Returns:
            Dict avec `export_url`, `export_id`
        """
        if not (start_time or "").strip() or not (end_time or "").strip():
            raise ValueError("start_time et end_time requis (ISO 8601)")
        if not (email or "").strip():
            raise ValueError("email requis (notification à un membre de l'équipe)")
        data = {"start_time": start_time, "end_time": end_time, "email": email}
        return self._request("POST", "conversations/export", json=data)

    def get_conversations_export(self, export_id: str) -> Dict[str, Any]:
        """
        Poll an export started by export_conversations.

        Args:
            export_id: id rendu par export_conversations

        Returns:
            Dict avec `redirect_url` (URL signée de téléchargement) une fois prêt
        """
        if not (export_id or "").strip():
            raise ValueError("export_id requis")
        return self._request("GET", f"conversations/export/{export_id}")
