"""PostHog API client — analytics produit (events, HogQL, personnes, insights,
feature flags, session recordings).

Bearer (`Authorization: Bearer phx_…`) sur l'API privée REST + l'endpoint
`/query/`. **Testé en live le 2026-08-22** contre un vrai projet PostHog Cloud
US (organisation Tulina, projet 571144) : identité, découverte de projet, HogQL,
schéma de base, et les 14 familles de ressources ci-dessous répondent exactement
comme codé. Les quatre points ci-dessous ne se déduisent PAS de la doc et ont
été établis par sonde.

**(1) TROIS types de clé, et la plus visible ne marche pas.** L'API privée
n'accepte que la clé **personnelle** `phx_…`. La clé de PROJET `phc_…` — celle
que PostHog met en avant (snippet JS, ingestion) — rend
`401 authentication_failed : "Personal API key found in request Authorization
header is invalid."` (vérifié en live avec le vrai token du projet). La clé
`phs_…` (project secret, bêta) ne porte pas les scopes analytiques. D'où le
refus À LA CONSTRUCTION d'un `phc_`/`phs_` : sans lui, la faute de
configuration la plus probable de ce connecteur se manifeste par un 401
indistinguable d'une clé révoquée.

**(2) La région fait partie de l'adresse, pas du compte.** `https://us.posthog.com`
et `https://eu.posthog.com` sont deux déploiements distincts ; une clé de l'un
est inconnue de l'autre, et le symptôme est encore un 401. Le host est donc un
champ de config NON secret apparié à la clé (même patron que `data_center` Zoho
ou `base_url` n8n), jamais une constante.

**(3) `project_id` est découvrable depuis la seule clé** — `GET /api/users/@me/`
rend `organization.teams[]`, chaque team portant son `id` numérique. Le champ de
config reste utile pour ÉPINGLER un projet quand la clé en voit plusieurs, mais
il n'est pas obligatoire : `resolve_project_id()` le résout sinon.

**(4) L'enveloppe de liste n'est PAS uniforme** (mesuré) : `/insights/` rend
`{count, next, previous, results}` (offset), `/persons/` rend
`{next, previous, results}` et `/events/` rend `{next, results}` — sans `count`.
Ne jamais supposer `count`. `next` est une **URL absolue** ; `next_page()` la
suit après avoir vérifié qu'elle pointe bien sur le host configuré (une URL
amont suivie aveuglément est un SSRF).

**Aucune écriture hors annotation.** Créer/modifier/basculer un feature flag,
écrire un insight ou une cohorte, supprimer une personne ou un enregistrement
n'existent pas ici — pas seulement « non exposés au niveau tool ». Basculer un
flag change le comportement du produit pour de vrais utilisateurs, et supprimer
une personne est irréversible et réglementaire (RGPD). Même doctrine que
`StripeClient` : une méthode absente force la décision à repasser par une PR.
L'annotation fait exception parce qu'elle est purement additive — c'est le
post-it « déploiement v2.3 ici » sur un graphe.

⚠️ **L'ingestion n'est pas ici non plus** (`/i/v0/e/`, `/batch/`) : elle
s'authentifie avec la clé de PROJET, pas la clé personnelle, et un agent qui
écrit des events dans le jeu de données sur lequel il rapporte corrompt sa
propre preuve. L'instrumentation appartient au SDK du produit.

Requires: requests
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import requests

from ...config import require_secret
from ..common import raise_for_upstream

_HTTP_TIMEOUT = (10, 120)  # (connexion, lecture) — une requête HogQL peut être longue
_DEFAULT_HOST = "https://us.posthog.com"

# Les hôtes PostHog Cloud connus. Une instance auto-hébergée est acceptée telle
# quelle (c'est le cas d'usage du champ `host`) — cette table ne sert qu'à
# rendre un message utile quand la clé d'une région est posée sur l'autre.
CLOUD_HOSTS = {
    "us": "https://us.posthog.com",
    "eu": "https://eu.posthog.com",
}


class PostHogClient:
    """Client PostHog (API privée REST + `/query/`), auth Bearer, clé personnelle."""

    def __init__(self, api_key: Optional[str] = None, *,
                 host: Optional[str] = None,
                 project_id: Optional[Any] = None):
        """
        Args:
            api_key: clé **personnelle** PostHog `phx_…` (ou variable d'env
                `POSTHOG_API_KEY`), créée dans Settings → Personal API keys.
                Une clé de projet `phc_…` ou une clé secrète de projet `phs_…`
                sont REFUSÉES ici (cf. le docstring du module).
            host: `https://us.posthog.com` (défaut), `https://eu.posthog.com`,
                ou l'URL d'une instance auto-hébergée. La région fait partie de
                l'adresse : une clé US est inconnue côté EU.
            project_id: projet par défaut. Facultatif — `resolve_project_id()`
                le découvre depuis la clé. À renseigner pour ÉPINGLER un projet
                quand la clé en voit plusieurs.
        """
        self.api_key = api_key or require_secret("POSTHOG_API_KEY")
        if self.api_key.startswith("phc_"):
            raise ValueError(
                "Clé de PROJET PostHog (`phc_…`) : c'est le jeton public d'ingestion, "
                "refusé par l'API de lecture (401 authentication_failed). Il faut une "
                "clé PERSONNELLE `phx_…` — PostHog → Settings → Personal API keys.")
        if self.api_key.startswith("phs_"):
            raise ValueError(
                "Clé secrète de projet PostHog (`phs_…`) : ses scopes ne couvrent pas "
                "la lecture analytique. Il faut une clé PERSONNELLE `phx_…` — "
                "PostHog → Settings → Personal API keys.")
        self.host = (host or _DEFAULT_HOST).rstrip("/")
        self.project_id = str(project_id) if project_id is not None else None
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {self.api_key}"
        self._resolved_project_id: Optional[str] = None

    # --- transport ----------------------------------------------------------

    def _request(self, method: str, path: str, *,
                 params: Optional[Dict[str, Any]] = None,
                 json_body: Any = None) -> Any:
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        resp = self.session.request(
            method, f"{self.host}{path}", params=clean or None, json=json_body,
            timeout=_HTTP_TIMEOUT)
        raise_for_upstream(resp, service="posthog")
        return resp.json() if resp.content else {}

    def _get(self, path: str, **params: Any) -> Any:
        return self._request("GET", path, params=params)

    def next_page(self, next_url: str) -> Any:
        """Suit le `next` d'une réponse paginée — c'est une URL ABSOLUE.

        L'URL est vérifiée contre le host configuré avant d'être suivie : suivre
        une URL rendue par l'amont sans la valider transformerait une réponse
        contrôlée par un tiers en requête sortante arbitraire (SSRF), avec notre
        en-tête `Authorization` dessus.
        """
        parsed = urlparse(next_url)
        expected = urlparse(self.host)
        if (parsed.scheme, parsed.netloc) != (expected.scheme, expected.netloc):
            raise ValueError(
                f"`next` pointe hors du host configuré ({parsed.scheme}://{parsed.netloc} "
                f"≠ {self.host}) — non suivi.")
        resp = self.session.get(next_url, timeout=_HTTP_TIMEOUT)
        raise_for_upstream(resp, service="posthog")
        return resp.json() if resp.content else {}

    # --- projet -------------------------------------------------------------

    def current_user(self) -> Any:
        """GET /api/users/@me/ — l'identité derrière la clé : email, organisation,
        et `organization.teams[]` (les projets visibles, avec leur `id`)."""
        return self._get("/api/users/@me/")

    def list_projects(self) -> Any:
        """GET /api/projects/ — les projets que cette clé peut lire."""
        return self._get("/api/projects/")

    def resolve_project_id(self) -> str:
        """Le projet sur lequel opérer : celui configuré, sinon le premier que la
        clé voit (`organization.teams[0].id`), mémorisé pour l'instance.

        Lève si la clé ne voit aucun projet — le cas d'une clé restreinte à une
        autre organisation, qui autrement échouerait plus loin sur un 404 opaque.
        """
        if self.project_id:
            return self.project_id
        if self._resolved_project_id:
            return self._resolved_project_id
        me = self.current_user()
        teams = (me.get("organization") or {}).get("teams") or me.get("teams") or []
        if not teams:
            raise ValueError(
                "Cette clé PostHog ne voit aucun projet — vérifie ses scopes "
                "(`project:read`) et sa restriction d'organisation/projet.")
        self._resolved_project_id = str(teams[0]["id"])
        return self._resolved_project_id

    def _p(self, project_id: Optional[Any] = None) -> str:
        """Le préfixe `/api/projects/{id}` de tout endpoint projet."""
        return f"/api/projects/{project_id if project_id is not None else self.resolve_project_id()}"

    # ================================================================
    # HogQL — l'endpoint qui répond à presque tout
    # ================================================================

    def query(self, hogql: str, *, project_id: Optional[Any] = None,
              **query_fields: Any) -> Any:
        """POST /api/projects/{id}/query/ — exécute du **HogQL** (SQL sur
        ClickHouse) et rend `{columns, types, results, hasMore, hogql, …}`.

        Args:
            hogql: la requête, ex.
                `SELECT event, count() AS n FROM events
                 WHERE timestamp > now() - INTERVAL 7 DAY
                 GROUP BY event ORDER BY n DESC LIMIT 20`.
                Les tables et colonnes se découvrent avec `database_schema()`.
            project_id: viser un autre projet que celui par défaut.
            **query_fields: champs additionnels de l'objet `query` (ex. `values`
                pour les placeholders, `filters`).

        Note: PostHog borne lui-même le résultat (`LIMIT 101 OFFSET 0` ajouté
        quand la requête n'a pas de LIMIT) et signale la troncature par
        `hasMore`. Une erreur de requête rend **400** avec
        `detail` = "Unable to resolve field: …" et
        `extra.hogql_metadata.errors[]` portant les offsets de caractères :
        c'est assez précis pour qu'un agent se corrige seul, donc ce message
        doit remonter tel quel plutôt qu'être remplacé par un texte générique.
        """
        body = {"query": {"kind": "HogQLQuery", "query": hogql, **query_fields}}
        return self._request("POST", f"{self._p(project_id)}/query/", json_body=body)

    def run_query(self, query: Dict[str, Any], *, project_id: Optional[Any] = None,
                  **body_fields: Any) -> Any:
        """POST /api/projects/{id}/query/ avec un objet `query` COMPLET — la voie
        des types de requête NOMMÉS de PostHog (`TrendsQuery`, `FunnelsQuery`,
        `RetentionQuery`, `StickinessQuery`, `LifecycleQuery`…), par opposition
        au SQL libre de `query()`.

        ⚠️ **À préférer à du HogQL écrit à la main pour les entonnoirs et la
        rétention.** La sémantique d'entonnoir de PostHog (étapes ordonnées ou
        non, fenêtre de conversion, étapes d'exclusion, attribution) ne se
        reconstitue pas fidèlement en SQL : on obtient un nombre plausible, et
        il ne correspond pas à celui que l'équipe lit sur son tableau de bord.
        Passer le type nommé fait calculer PostHog, donc le MÊME nombre que l'UI.

        Args:
            query: l'objet de requête, `kind` compris.
            **body_fields: champs de corps additionnels (`refresh`,
                `client_query_id`, `filters_override`).
        """
        if not isinstance(query, dict) or not query.get("kind"):
            raise ValueError("`query` doit être un dict portant un `kind` "
                             "(HogQLQuery, TrendsQuery, FunnelsQuery, RetentionQuery…).")
        return self._request("POST", f"{self._p(project_id)}/query/",
                             json_body={"query": query, **body_fields})

    def run_insight(self, insight_id: Any, *, date_from: Optional[str] = None,
                    date_to: Optional[str] = None,
                    project_id: Optional[Any] = None) -> Any:
        """Ré-exécute un insight SAUVEGARDÉ, éventuellement sur une autre fenêtre.

        Lit la définition de l'insight puis rejoue SA propre requête via
        `/query/`. C'est « notre entonnoir, mais sur la semaine dernière » sans
        que rien ne soit réinterprété : la définition vient de l'équipe, le
        calcul vient de PostHog, donc le chiffre rendu est celui du tableau de
        bord. À préférer systématiquement à une reconstitution en HogQL.

        Args:
            insight_id: l'id (ou le `short_id`) de l'insight.
            date_from/date_to: fenêtre de remplacement, syntaxe PostHog
                (`-7d`, `-30d`, `mStart`, `yStart`, ou une date ISO). Omis =
                la fenêtre enregistrée avec l'insight.
        """
        insight = self.get_insight(insight_id, project_id=project_id)
        query = insight.get("query")
        if not query:
            raise ValueError(
                f"L'insight {insight_id} n'a pas de `query` réexécutable — c'est un "
                "insight au format hérité (`filters`), que PostHog ne rejoue pas par "
                "cette voie. Ouvre-le dans l'UI, ou reformule la question en HogQL.")
        source = query.get("source") if isinstance(query.get("source"), dict) else query
        if date_from is not None or date_to is not None:
            date_range = dict(source.get("dateRange") or {})
            if date_from is not None:
                date_range["date_from"] = date_from
            if date_to is not None:
                date_range["date_to"] = date_to
            source["dateRange"] = date_range
        return self.run_query(query, project_id=project_id)

    def database_schema(self, project_id: Optional[Any] = None) -> Any:
        """POST /api/projects/{id}/query/ `{kind: DatabaseSchemaQuery}` — les
        tables interrogeables en HogQL et leurs colonnes.

        ⚠️ Volumineux : 156 tables sur un projet neuf, dont `events` à 52
        colonnes (mesuré le 2026-08-22). À projeter avant de rendre à un agent —
        les noms de tables d'abord, les colonnes d'UNE table ensuite.
        """
        return self._request("POST", f"{self._p(project_id)}/query/",
                             json_body={"query": {"kind": "DatabaseSchemaQuery"}})

    # ================================================================
    # Définitions — le vocabulaire du projet
    # ================================================================

    def list_event_definitions(self, project_id: Optional[Any] = None, **params: Any) -> Any:
        """GET {p}/event_definitions/ — les types d'events connus du projet.
        `search` filtre par nom, `limit`/`offset` paginent."""
        return self._get(f"{self._p(project_id)}/event_definitions/", **params)

    def list_property_definitions(self, project_id: Optional[Any] = None, **params: Any) -> Any:
        """GET {p}/property_definitions/ — les propriétés connues.
        `search`, `type` (event | person | group), `event_names`, `limit`."""
        return self._get(f"{self._p(project_id)}/property_definitions/", **params)

    def list_property_values(self, key: str, project_id: Optional[Any] = None,
                             **params: Any) -> Any:
        """GET {p}/persons/properties/{key}/values/ — les valeurs observées
        d'une propriété (pour proposer un filtre plausible plutôt qu'inventé)."""
        return self._get(f"{self._p(project_id)}/persons/properties/{key}/values/",
                         **params)

    # ================================================================
    # Events & personnes
    # ================================================================

    def list_events(self, project_id: Optional[Any] = None, **params: Any) -> Any:
        """GET {p}/events/ — events bruts, le plus récent d'abord.

        Args:
            **params: `event` (nom), `distinct_id`, `person_id`, `after`/`before`
                (ISO 8601), `properties` (JSON), `limit`.

        ⚠️ Enveloppe `{next, results}` — **pas de `count`**. Pour compter ou
        agréger, passer par `query()` : compter en paginant des events bruts est
        à la fois lent et faux dès qu'il y a plus d'une page.
        """
        return self._get(f"{self._p(project_id)}/events/", **params)

    def get_event(self, event_id: str, project_id: Optional[Any] = None) -> Any:
        """GET {p}/events/{id}/."""
        return self._get(f"{self._p(project_id)}/events/{event_id}/")

    def list_persons(self, project_id: Optional[Any] = None, **params: Any) -> Any:
        """GET {p}/persons/ — filtres `search` (email/nom/distinct_id),
        `properties` (JSON), `cohort`, `distinct_id`, `limit`.
        Enveloppe `{next, previous, results}`, sans `count`."""
        return self._get(f"{self._p(project_id)}/persons/", **params)

    def get_person(self, person_id: Any, project_id: Optional[Any] = None) -> Any:
        """GET {p}/persons/{id}/ — la fiche d'une personne et ses propriétés."""
        return self._get(f"{self._p(project_id)}/persons/{person_id}/")

    def list_person_activity(self, person_id: Any, project_id: Optional[Any] = None,
                             **params: Any) -> Any:
        """GET {p}/persons/{id}/activity/ — l'activité d'une personne."""
        return self._get(f"{self._p(project_id)}/persons/{person_id}/activity/", **params)

    # ================================================================
    # Insights, dashboards, cohortes — le travail déjà sauvegardé
    # ================================================================

    def list_insights(self, project_id: Optional[Any] = None, **params: Any) -> Any:
        """GET {p}/insights/ — les insights sauvegardés. `search`, `saved`,
        `favorited`, `limit`/`offset`. Enveloppe `{count, next, previous, results}`."""
        return self._get(f"{self._p(project_id)}/insights/", **params)

    def get_insight(self, insight_id: Any, project_id: Optional[Any] = None) -> Any:
        """GET {p}/insights/{id}/ — la définition d'un insight."""
        return self._get(f"{self._p(project_id)}/insights/{insight_id}/")

    def list_dashboards(self, project_id: Optional[Any] = None, **params: Any) -> Any:
        """GET {p}/dashboards/ — les tableaux de bord et leurs tuiles."""
        return self._get(f"{self._p(project_id)}/dashboards/", **params)

    def get_dashboard(self, dashboard_id: Any, project_id: Optional[Any] = None) -> Any:
        """GET {p}/dashboards/{id}/."""
        return self._get(f"{self._p(project_id)}/dashboards/{dashboard_id}/")

    def list_cohorts(self, project_id: Optional[Any] = None, **params: Any) -> Any:
        """GET {p}/cohorts/ — les cohortes définies."""
        return self._get(f"{self._p(project_id)}/cohorts/", **params)

    def get_cohort(self, cohort_id: Any, project_id: Optional[Any] = None) -> Any:
        """GET {p}/cohorts/{id}/."""
        return self._get(f"{self._p(project_id)}/cohorts/{cohort_id}/")

    def list_cohort_persons(self, cohort_id: Any, project_id: Optional[Any] = None,
                            **params: Any) -> Any:
        """GET {p}/cohorts/{id}/persons/ — qui est DANS une cohorte."""
        return self._get(f"{self._p(project_id)}/cohorts/{cohort_id}/persons/", **params)

    def list_actions(self, project_id: Optional[Any] = None, **params: Any) -> Any:
        """GET {p}/actions/ — les actions (events nommés par l'équipe)."""
        return self._get(f"{self._p(project_id)}/actions/", **params)

    # ================================================================
    # Groupes — l'analytics B2B (comptes, organisations)
    # ================================================================

    def list_group_types(self, project_id: Optional[Any] = None) -> Any:
        """GET {p}/groups_types/ — les TYPES de groupe définis (« company »,
        « workspace »…) avec leur `group_type_index`.

        À appeler en premier : sans l'index, aucun appel groupe n'est possible.
        Une liste vide signifie que le projet ne fait pas d'analytics de groupe —
        les questions par COMPTE (« quels clients décrochent ») n'ont alors pas
        de réponse ici, et il faut le dire plutôt que de répondre par personne.
        ⚠️ Rend une liste NUE, pas l'enveloppe `{results}` (mesuré le 22/08/2026).
        """
        return self._get(f"{self._p(project_id)}/groups_types/")

    def list_groups(self, group_type_index: int, project_id: Optional[Any] = None,
                    **params: Any) -> Any:
        """GET {p}/groups/ — les groupes d'un type donné (les COMPTES, en B2B).

        Args:
            group_type_index: l'index rendu par `list_group_types()`.
            **params: `search`, `cursor`.
        """
        return self._get(f"{self._p(project_id)}/groups/",
                         group_type_index=group_type_index, **params)

    def find_group(self, group_type_index: int, group_key: str,
                   project_id: Optional[Any] = None) -> Any:
        """GET {p}/groups/find/ — un groupe précis par sa clé métier."""
        return self._get(f"{self._p(project_id)}/groups/find/",
                         group_type_index=group_type_index, group_key=group_key)

    # ================================================================
    # Feature flags, expériences — LECTURE seule
    # ================================================================

    def list_feature_flags(self, project_id: Optional[Any] = None, **params: Any) -> Any:
        """GET {p}/feature_flags/ — les flags, leur `key`, leur état `active` et
        leurs conditions de déploiement. En créer, modifier ou basculer un
        n'existe pas ici : un flag basculé change le produit pour de vrais
        utilisateurs (cf. docstring du module)."""
        return self._get(f"{self._p(project_id)}/feature_flags/", **params)

    def get_feature_flag(self, flag_id: Any, project_id: Optional[Any] = None) -> Any:
        """GET {p}/feature_flags/{id}/."""
        return self._get(f"{self._p(project_id)}/feature_flags/{flag_id}/")

    def list_experiments(self, project_id: Optional[Any] = None, **params: Any) -> Any:
        """GET {p}/experiments/ — les tests A/B et leurs métriques."""
        return self._get(f"{self._p(project_id)}/experiments/", **params)

    def get_experiment(self, experiment_id: Any, project_id: Optional[Any] = None) -> Any:
        """GET {p}/experiments/{id}/."""
        return self._get(f"{self._p(project_id)}/experiments/{experiment_id}/")

    # ================================================================
    # Session recordings, sondages
    # ================================================================

    def list_session_recordings(self, project_id: Optional[Any] = None, **params: Any) -> Any:
        """GET {p}/session_recordings/ — filtres `date_from`/`date_to`,
        `person_uuid`, `limit`. Enveloppe `{next, results}`, sans `count`."""
        return self._get(f"{self._p(project_id)}/session_recordings/", **params)

    def get_session_recording(self, recording_id: str,
                              project_id: Optional[Any] = None, **params: Any) -> Any:
        """GET {p}/session_recordings/{id}/ — métadonnées d'un enregistrement
        (durée, personne, URLs visitées). PAS la vidéo."""
        return self._get(f"{self._p(project_id)}/session_recordings/{recording_id}/",
                         **params)

    def list_surveys(self, project_id: Optional[Any] = None, **params: Any) -> Any:
        """GET {p}/surveys/ — les sondages in-app et leur état."""
        return self._get(f"{self._p(project_id)}/surveys/", **params)

    # ================================================================
    # Annotations — la SEULE écriture
    # ================================================================

    def list_annotations(self, project_id: Optional[Any] = None, **params: Any) -> Any:
        """GET {p}/annotations/ — les repères posés sur la frise temporelle."""
        return self._get(f"{self._p(project_id)}/annotations/", **params)

    def create_annotation(self, content: str, *, date_marker: Optional[str] = None,
                          project_id: Optional[Any] = None, **body: Any) -> Any:
        """POST {p}/annotations/ — pose un repère daté sur les graphes du projet
        (« déploiement v2.3 », « début de campagne »). Purement ADDITIF : ça
        n'altère aucune donnée mesurée, seulement leur lecture — d'où la seule
        écriture retenue de ce connecteur.

        Args:
            content: le texte du repère.
            date_marker: l'instant repéré (ISO 8601). Défaut PostHog = maintenant.
            **body: `scope` ("project" | "organization"), `dashboard_item`
                (épingler à un insight précis).
        """
        payload = {"content": content, **body}
        if date_marker is not None:
            payload["date_marker"] = date_marker
        return self._request("POST", f"{self._p(project_id)}/annotations/",
                             json_body=payload)
