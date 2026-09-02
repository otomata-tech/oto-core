"""GitHub REST API client — dépôts, issues, pull requests, organisations, Actions.

API REST v3 (`https://api.github.com`, doc https://docs.github.com/en/rest), auth
**Bearer** (jeton personnel, jeton d'installation d'app, ou `GITHUB_TOKEN` d'un
workflow). Une méthode = un endpoint ; corps et réponses passent tels quels.
Conventions relevées dans la documentation éditeur le 2026-09-02.

Ce module porte la **construction et le transport**, et compose les familles
d'appels de `_api/` (dépôts, issues, pull requests, organisations, Actions,
recherche). Les constantes vivent dans `const.py` et sont réexportées ici.

Six choses conditionnent l'appelant, et la moitié sont des pièges silencieux :

- ⚠️ **`per_page` plafonne à 100 et GitHub RABOTE EN SILENCE au-delà** : pas
  d'erreur, juste moins de lignes que demandé. Un appelant qui demande 500 croit
  avoir tout lu et n'a que les 100 premiers. `_check_per_page` refuse donc
  localement, en nommant la borne.

- ⚠️ **Toutes les listes ne sont pas des tableaux.** La recherche rend
  `{total_count, items: [...]}`, Actions rend `{total_count, workflow_runs: […]}`,
  `{jobs: […]}`, `{artifacts: […]}`. Une boucle de pagination écrite pour un
  tableau nu rate ces endpoints, ou pire, itère sur les CLÉS du dict. `iterate()`
  écrit la règle une fois : il suit l'en-tête `Link` et sait extraire la liste
  quelle que soit son enveloppe.

- ⚠️ **La recherche est bornée à 1 000 résultats**, quelle que soit la
  pagination : `total_count` peut annoncer 12 000 et la 11ᵉ page répondre 422.
  `total_count` n'est donc PAS le nombre de lignes récupérables.

- ⚠️ **Un 404 ne veut pas dire « n'existe pas ».** Sur une ressource privée que
  le jeton n'a pas le droit de voir, GitHub répond 404 plutôt que 403, exprès,
  pour ne pas divulguer son existence. Un dépôt privé « introuvable » est le plus
  souvent un problème de *scope* du jeton, pas de nom.

- **Deux limites d'usage, pas une.** La primaire est décrite par les en-têtes
  `x-ratelimit-*`. La « secondaire » (anti-abus) frappe les rafales d'écritures
  et répond 403 ou 429 avec `Retry-After`. Les deux sont retentées ici — **en
  lecture seule** : l'API REST n'offre aucune clé d'idempotence, et rejouer un
  POST créerait une seconde issue, un second commentaire, un second commit.

- **GitHub Enterprise Server** se sert par `base_url` (typiquement
  `https://<host>/api/v3`). Le reste du client est identique.

**Délibérément absent** (hors périmètre, à ne pas « compléter » sans décision) :
la suppression d'un dépôt et celle d'une organisation — deux gestes destructeurs
et irréversibles qu'aucun cas d'usage de ce connecteur ne réclame ; la gestion
des secrets et variables Actions (les poser par un connecteur reviendrait à
déplacer des credentials d'un coffre à un autre) ; l'administration GitHub App ;
et la facturation.

Requires: requests
"""
from __future__ import annotations

import re
import time
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

import requests

from ...config import require_secret
from ..common import raise_for_upstream
from ._api import (_ActionsMixin, _IssuesMixin, _OrgsMixin, _PullsMixin,
                   _ReposMixin, _SearchMixin)
from .const import (COLLABORATOR_PERMISSIONS, DEFAULT_ACCEPT,
                    DEFAULT_API_VERSION, DEFAULT_BASE_URL, DEFAULT_PER_PAGE,
                    HTTP_TIMEOUT, ISSUE_SORTS, ISSUE_STATE_WRITES,
                    ISSUE_STATES, MAX_ATTEMPTS, MAX_PER_PAGE, MEMBER_FILTERS,
                    MEMBERSHIP_ROLES, MERGE_METHODS, MIN_PER_PAGE,
                    ORG_REPO_TYPES, PULL_SORTS, PULL_STATES, REPO_SORTS,
                    REPO_TYPES, RETRY_STATUSES, REVIEW_EVENTS, RUN_STATUSES,
                    SEARCH_CODE_SORTS, SEARCH_ISSUE_SORTS, SEARCH_MAX_RESULTS,
                    SEARCH_REPO_SORTS, SORT_DIRECTIONS, TEAM_ROLES)

#: Les clés sous lesquelles GitHub range une liste quand la réponse est un OBJET
#: et non un tableau. Écrites une fois : `iterate()` s'en sert pour trouver les
#: lignes sans que chaque appelant ait à savoir quelle enveloppe l'attend.
_LIST_KEYS = ("items", "workflow_runs", "workflows", "jobs", "artifacts",
              "repositories", "check_runs", "check_suites", "secrets",
              "installations", "users", "commits")

_LINK_NEXT_RE = re.compile(r'<([^>]+)>\s*;\s*rel="next"')


class GitHubClient(
    _ReposMixin,
    _IssuesMixin,
    _PullsMixin,
    _OrgsMixin,
    _ActionsMixin,
    _SearchMixin,
):
    """Client GitHub REST v3 (https://api.github.com), auth Bearer."""

    def __init__(self, token: Optional[str] = None,
                 base_url: Optional[str] = None,
                 api_version: Optional[str] = None):
        """
        Args:
            token: jeton GitHub (ou env `GITHUB_TOKEN`). Jeton personnel
                (classique ou « fine-grained »), jeton d'installation d'app, ou
                le jeton éphémère d'un workflow Actions.
            base_url: racine de l'API. Défaut `https://api.github.com` ; pour un
                GitHub Enterprise Server, `https://<host>/api/v3`.
            api_version: valeur de l'en-tête `X-GitHub-Api-Version`
                (défaut `DEFAULT_API_VERSION`).

        ⚠️ Ce que le jeton PEUT dépend de ses scopes (jeton classique) ou de ses
        permissions et de sa liste de dépôts (jeton fine-grained). Un droit
        manquant se manifeste souvent en **404**, pas en 403 : cf. l'en-tête de
        module.
        """
        self.token = token or require_secret("GITHUB_TOKEN")
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.api_version = api_version or DEFAULT_API_VERSION
        #: En-tête `Link` de la dernière réponse (cf. `_request`/`iterate`).
        self._last_link: Optional[str] = None
        self.session = requests.Session()
        # Jeton en HEADER uniquement (jamais en query string : il finirait dans
        # l'URL, donc dans le message de toute exception, les logs et Sentry).
        self.session.headers.update({
            "Authorization": f"Bearer {self.token}",
            "Accept": DEFAULT_ACCEPT,
            "X-GitHub-Api-Version": self.api_version,
        })

    # --- transport ----------------------------------------------------------

    @staticmethod
    def _check_per_page(per_page: Optional[int]) -> None:
        """`per_page` hors [1, 100] est refusé ICI.

        ⚠️ GitHub ne renverrait PAS d'erreur : il rabote en silence à 100. Le
        refus local est donc la seule façon de distinguer « j'ai tout » de « j'ai
        les cent premiers ».
        """
        if per_page is None:
            return
        if not isinstance(per_page, int) or isinstance(per_page, bool):
            raise ValueError("`per_page` doit être un entier.")
        if not (MIN_PER_PAGE <= per_page <= MAX_PER_PAGE):
            raise ValueError(
                f"`per_page` doit être entre {MIN_PER_PAGE} et {MAX_PER_PAGE} "
                f"(plafond GitHub) ; reçu {per_page}. ⚠️ GitHub raboterait à "
                f"{MAX_PER_PAGE} SANS erreur — pour aller au-delà, paginer avec "
                "`page`, ou boucler avec `iterate`.")

    @staticmethod
    def _check_choice(name: str, value: Optional[Any],
                      allowed: Iterable[Any]) -> None:
        """Refuse localement une valeur hors énumération, en NOMMANT les valides."""
        if value is None:
            return
        allowed = tuple(allowed)
        if value not in allowed:
            raise ValueError(
                f"`{name}` invalide : {value!r}. Valeurs acceptées : "
                + ", ".join(repr(a) for a in allowed))

    @staticmethod
    def _encode_params(params: Optional[Dict[str, Any]]) -> List[Tuple[str, Any]]:
        """Params → paires, `None` retiré, booléens en `true`/`false`, listes
        jointes par des virgules (forme lue par GitHub pour `labels`, `assignees`
        en filtre, etc.)."""
        out: List[Tuple[str, Any]] = []
        for key, value in (params or {}).items():
            if value is None:
                continue
            if isinstance(value, bool):
                out.append((key, "true" if value else "false"))
            elif isinstance(value, (list, tuple)):
                out.append((key, ",".join(str(v) for v in value)))
            else:
                out.append((key, value))
        return out

    @staticmethod
    def _retry_after(resp: Any, attempt: int) -> float:
        """Délai avant re-tentative.

        `Retry-After` d'abord (l'amont sait mieux que nous — c'est notamment ce
        que porte la limite secondaire anti-abus). Sinon, si la limite primaire
        est épuisée (`x-ratelimit-remaining: 0`), attendre la réinitialisation
        annoncée. Sinon, backoff exponentiel.
        """
        headers = getattr(resp, "headers", None) or {}
        raw = headers.get("Retry-After")
        if raw:
            try:
                return max(0.0, float(raw))
            except (TypeError, ValueError):
                pass
        if str(headers.get("x-ratelimit-remaining", "")).strip() == "0":
            reset = headers.get("x-ratelimit-reset")
            if reset:
                try:
                    # Borné : un `reset` lointain (ou une horloge décalée) ne doit
                    # pas geler l'appelant pendant une heure.
                    return max(0.0, min(60.0, float(reset) - time.time()))
                except (TypeError, ValueError):
                    pass
        return float(2 ** attempt)

    def _is_retryable_status(self, resp: Any) -> bool:
        """403 n'est retentable QUE s'il porte la marque d'une limite d'usage.

        GitHub sert le même code pour « ton jeton n'a pas le droit » (définitif,
        retenter est inutile) et pour la limite secondaire anti-abus (passager).
        Les distinguer évite de marteler une permission manquante trois fois.
        """
        status = resp.status_code
        if status in RETRY_STATUSES:
            return True
        if status != 403:
            return False
        headers = getattr(resp, "headers", None) or {}
        if headers.get("Retry-After"):
            return True
        return str(headers.get("x-ratelimit-remaining", "")).strip() == "0"

    def _request(self, method: str, path: str, *,
                 params: Optional[Dict[str, Any]] = None,
                 json: Any = None, accept: Optional[str] = None,
                 raw: bool = False) -> Any:
        """Une requête. `raw=True` rend la RÉPONSE (pas son JSON) — pour les
        endpoints qui servent un binaire ou une redirection (logs, artefacts)."""
        encoded = self._encode_params(params)
        headers = {"Accept": accept} if accept else None
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        # Retente en LECTURE seulement : l'API REST n'offre aucune clé
        # d'idempotence, donc rejouer un POST créerait une seconde issue, un
        # second commentaire, un second commit.
        retryable = method.upper() in ("GET", "HEAD")
        last = None
        for attempt in range(MAX_ATTEMPTS):
            last = self.session.request(
                method, url, params=encoded or None, json=json,
                headers=headers, timeout=HTTP_TIMEOUT,
                allow_redirects=not raw)
            if (not self._is_retryable_status(last) or not retryable
                    or attempt == MAX_ATTEMPTS - 1):
                break
            time.sleep(self._retry_after(last, attempt))
        # L'en-tête `Link` de la DERNIÈRE réponse : c'est lui qui dit s'il reste
        # une page, et `iterate()` le relit. Posé ici, au seul endroit qui voit
        # la réponse — sinon chaque famille d'appels devrait le faire remonter.
        self._last_link = (getattr(last, "headers", None) or {}).get("Link")
        if raw:
            return last
        raise_for_upstream(last, service="github")
        if not last.content:
            return {}
        try:
            return last.json()
        except ValueError:
            return last.text

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None,
             per_page: Optional[int] = None,
             page: Optional[int] = None) -> Any:
        """GET paginé : borne `per_page` puis passe `page`."""
        self._check_per_page(per_page)
        merged: Dict[str, Any] = dict(params or {})
        merged.update({"per_page": per_page, "page": page})
        return self._request("GET", path, params=merged)

    # --- pagination ---------------------------------------------------------

    @staticmethod
    def _rows(payload: Any) -> List[Any]:
        """Les LIGNES d'une réponse de liste, quelle que soit son enveloppe.

        Un tableau nu est rendu tel quel. Un objet est fouillé selon `_LIST_KEYS`
        (`items` pour la recherche, `workflow_runs`/`jobs`/`artifacts` pour
        Actions…). Sans cette normalisation, une boucle écrite pour un tableau
        itérerait sur les CLÉS du dict — et rendrait des chaînes au lieu de
        lignes, sans lever.
        """
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            for key in _LIST_KEYS:
                value = payload.get(key)
                if isinstance(value, list):
                    return value
        return []

    def iterate(self, method: Any, *args: Any, max_pages: Optional[int] = None,
                **kwargs: Any) -> Iterator[Any]:
        """Déroule une liste paginée et rend les LIGNES, page après page.

        Écrit une fois deux règles que chaque appelant réécrirait mal : suivre
        l'en-tête `Link` (`rel="next"`) plutôt que d'incrémenter `page` à
        l'aveugle, et extraire les lignes même quand la réponse est un OBJET
        (recherche, Actions) plutôt qu'un tableau.

        `method` est une méthode de liste de ce client ::

            for issue in client.iterate(client.list_issues, "octo", "repo",
                                        state="open"):
                ...

        `max_pages` borne le déroulé — utile quand l'appelant sert un agent et
        doit tenir un budget de réponse.

        ⚠️ **Sur la recherche, GitHub s'arrête à 1 000 résultats** et répond 422
        au-delà : cette boucle s'arrête donc d'elle-même, mais `total_count` aura
        pu annoncer bien plus. Ce n'est pas une page perdue, c'est la borne de
        l'API.

        ⚠️ Ne pas passer `page` : c'est cette boucle qui le gère.
        """
        if "page" in kwargs:
            raise ValueError(
                "`iterate` gère la pagination lui-même — ne pas passer `page`.")
        pages = 0
        page = 1
        while True:
            payload = method(*args, page=page, **kwargs)
            rows = self._rows(payload)
            for row in rows:
                yield row
            pages += 1
            if max_pages is not None and pages >= max_pages:
                return
            # `Link` fait autorité quand il est là ; sinon on s'arrête sur une
            # page vide ou incomplète. `_last_link` est posé par `_request` via
            # la session — voir `_capture_link`.
            if not rows or not self._has_next_page():
                return
            page += 1

    def _has_next_page(self) -> bool:
        """La dernière réponse annonçait-elle une page suivante (`Link` `rel=next`) ?

        GitHub omet `Link` quand tout tient sur une page : son absence vaut donc
        « c'est fini », et c'est une information, pas un manque.
        """
        link = getattr(self, "_last_link", None)
        return bool(link and _LINK_NEXT_RE.search(link))


__all__ = [
    "GitHubClient",
    "DEFAULT_BASE_URL", "DEFAULT_ACCEPT", "DEFAULT_API_VERSION",
    "HTTP_TIMEOUT", "MIN_PER_PAGE", "MAX_PER_PAGE", "DEFAULT_PER_PAGE",
    "SEARCH_MAX_RESULTS", "RETRY_STATUSES", "MAX_ATTEMPTS",
    "ISSUE_STATES", "ISSUE_STATE_WRITES", "ISSUE_SORTS", "SORT_DIRECTIONS",
    "PULL_STATES", "PULL_SORTS", "MERGE_METHODS", "REVIEW_EVENTS",
    "REPO_TYPES", "REPO_SORTS", "ORG_REPO_TYPES", "RUN_STATUSES",
    "MEMBERSHIP_ROLES", "MEMBER_FILTERS", "TEAM_ROLES",
    "COLLABORATOR_PERMISSIONS",
    "SEARCH_CODE_SORTS", "SEARCH_REPO_SORTS", "SEARCH_ISSUE_SORTS",
]
