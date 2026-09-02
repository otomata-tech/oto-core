"""Productlane API client — retours clients, roadmap, centre d'aide.

API **v2** (`https://productlane.com/api/v2`, doc https://productlane.mintlify.dev),
auth **Bearer**. Une méthode = un endpoint ; corps et réponses passent tels quels,
le client n'invente aucune sémantique. Chemins, verbes, paramètres et scopes
relevés dans l'OpenAPI publié par l'éditeur (`openapi-v2.json`) le 2026-09-02.

Ce module porte la **construction et le transport**, et compose les familles
d'appels de `_api/` (fils, contacts, entreprises, roadmap, changelogs, docs,
taxonomie, méta). Les constantes vivent dans `const.py` et sont réexportées ici :
le backend épingle oto-core par tag et n'importe que
`oto.tools.productlane.client`.

Cinq choses conditionnent l'appelant :

- **Pagination par CURSEUR, partout, sans exception.** Ni `page`, ni `offset`,
  ni `skip` nulle part. Une liste rend `{data: [...], page: {cursor, has_more,
  limit}}` ; la boucle se fait sur `has_more`, pas sur la taille de `data`
  (« Empty array on the last page if it lined up »). `iterate()` écrit cette
  boucle une fois — la recopier au site d'appel est le moyen le plus simple de
  perdre une page. Tri figé côté serveur (`created_at DESC, id DESC`), sans
  paramètre pour en changer.

- ⚠️ **`limit` plafonne à 200** (défaut 50). `_check_limit` refuse localement
  hors bornes plutôt que de laisser partir un 400.

- ⚠️ **Productlane est un MIROIR de Linear pour sa roadmap.** Projets et issues
  sont créés dans Linear d'abord ; les mises à jour et suppressions y sont
  poussées, **et un échec de cette synchro ne fait PAS échouer l'appel** (il est
  journalisé côté éditeur). Un `200` sur `update_issue` ne prouve donc pas que
  Linear a suivi. Voir `_api/roadmap.py`.

- ⚠️ **Un seul appel de tout ce client écrit à des tiers** :
  `broadcast_changelog` (email aux contacts abonnés et/ou publication Slack).
  Sans annulation ni rappel possible. Il est traité à part dans
  `_api/changelogs.py` — signature explicite, refus local si aucun canal.

- **Les énumérations sont scopées à leur endpoint** : `status` ne vaut pas la
  même chose sur un fil et sur un brouillon de doc, `type` pas la même chose sur
  un expéditeur bloqué et sur un message. `const.py` en donne la raison, et porte
  un nom par usage plutôt qu'un nom par paramètre.

Limites amont **par clé** : 1000 GET/minute, 60 écritures/minute, burst 2× sur
10 s. Chaque réponse porte `X-RateLimit-{Limit,Remaining,Reset}` ; le 429 porte
`Retry-After`, respecté par la boucle de re-tentative — **en lecture seule**,
l'API n'offrant aucune clé d'idempotence.

**Hors périmètre, délibérément** (à ne pas « compléter » sans décision) : toute
l'administration de l'espace de travail — `/members`, invitations, changement de
rôle, retrait d'un membre. Elle exige le scope `admin`, envoie des emails
d'invitation, et n'a rien à faire dans un connecteur de retours clients.

Requires: requests
"""
from __future__ import annotations

import time
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

import requests

from ...config import require_secret
from ..common import raise_for_upstream
from ._api import (_ChangelogsMixin, _CompaniesMixin, _ContactsMixin,
                   _DocsMixin, _MetaMixin, _RoadmapMixin, _TaxonomyMixin,
                   _ThreadsMixin)
from .const import (BLOCKED_SENDER_TYPES, DEFAULT_LIMIT, DOC_KIND_FILTERS,
                    DOC_KINDS, DOC_VISIBILITIES, DOC_VISIBILITY_FILTERS,
                    DRAFT_KINDS, DRAFT_STATUSES, HTTP_TIMEOUT, ISSUE_PRIORITIES,
                    MAX_ATTEMPTS, MAX_LIMIT, MESSAGE_DIRECTIONS,
                    MESSAGE_ORDERS, MESSAGE_TYPES, MIN_LIMIT, PAIN_LEVELS,
                    PROJECT_STATES, RETRY_STATUSES, ROADMAP_SORTS,
                    THREAD_EXPANDS, THREAD_ORIGINS, THREAD_STATUSES,
                    THREAD_TABS)


class ProductlaneClient(
    _MetaMixin,
    _ThreadsMixin,
    _ContactsMixin,
    _CompaniesMixin,
    _RoadmapMixin,
    _ChangelogsMixin,
    _DocsMixin,
    _TaxonomyMixin,
):
    """Client Productlane v2 (https://productlane.com/api/v2), auth Bearer."""

    BASE_URL = "https://productlane.com/api/v2"

    def __init__(self, api_key: Optional[str] = None):
        """
        Args:
            api_key: clé d'API v2 Productlane (ou env `PRODUCTLANE_API_KEY`).

        La clé se génère dans Productlane (Settings → API). ⚠️ Une clé **v1** ne
        marche pas ici : v2 est une API distincte, et v1 s'arrête le 20/11/2026.
        """
        self.api_key = api_key or require_secret("PRODUCTLANE_API_KEY")
        self.session = requests.Session()
        # Clé en HEADER uniquement (jamais en query string : elle finirait dans
        # l'URL, donc dans le message de toute exception, les logs et Sentry).
        self.session.headers.update({
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        })

    # --- transport ----------------------------------------------------------

    @staticmethod
    def _check_limit(limit: Optional[int]) -> None:
        """`limit` hors [1, 200] est refusé ICI. L'API rendrait un 400 ; le dire
        localement nomme la borne réelle, que personne ne devine."""
        if limit is None:
            return
        if not isinstance(limit, int) or isinstance(limit, bool):
            raise ValueError("`limit` doit être un entier.")
        if not (MIN_LIMIT <= limit <= MAX_LIMIT):
            raise ValueError(
                f"`limit` doit être entre {MIN_LIMIT} et {MAX_LIMIT} "
                f"(plafond de l'API Productlane) ; reçu {limit}. "
                "Au-delà, paginer avec `cursor` (ou boucler avec `iterate`).")

    @staticmethod
    def _check_choice(name: str, value: Optional[Any],
                      allowed: Iterable[Any]) -> None:
        """Refuse localement une valeur hors énumération, en NOMMANT les valides.

        ⚠️ Les énumérations sont passées PAR L'APPELANT, depuis `const.py` :
        le même nom de paramètre n'a pas les mêmes valeurs partout (cf. l'en-tête
        de `const.py`), donc ce garde ne peut pas les deviner du nom.
        """
        if value is None:
            return
        allowed = tuple(allowed)
        if value not in allowed:
            raise ValueError(
                f"`{name}` invalide : {value!r}. Valeurs acceptées : "
                + ", ".join(repr(a) for a in allowed))

    @staticmethod
    def _encode_params(params: Optional[Dict[str, Any]]) -> List[Tuple[str, Any]]:
        """Params → liste de paires, `None` retiré, booléens en `true`/`false`.

        (requests écrirait `True`, que le serveur ne lit pas comme un booléen.)
        Une liste est jointe par des virgules : c'est la forme que l'API v2 lit
        pour ses rares paramètres multi-valeurs (`expand`).
        """
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
        """Délai avant re-tentative : `Retry-After` s'il est là (l'amont sait mieux
        que nous), sinon backoff exponentiel."""
        raw = (getattr(resp, "headers", None) or {}).get("Retry-After")
        if raw:
            try:
                return max(0.0, float(raw))
            except (TypeError, ValueError):
                pass
        return float(2 ** attempt)

    def _request(self, method: str, path: str, *,
                 params: Optional[Dict[str, Any]] = None,
                 json: Any = None) -> Any:
        encoded = self._encode_params(params)
        # Retente 429/5xx en LECTURE seulement : l'API n'offre AUCUNE clé
        # d'idempotence, donc rejouer un POST créerait un doublon — un fil de
        # plus, ou pire, une diffusion de changelog envoyée deux fois.
        retryable = method.upper() in ("GET", "HEAD")
        last = None
        for attempt in range(MAX_ATTEMPTS):
            last = self.session.request(
                method, f"{self.BASE_URL}{path}", params=encoded or None,
                json=json, timeout=HTTP_TIMEOUT)
            if (last.status_code not in RETRY_STATUSES
                    or not retryable or attempt == MAX_ATTEMPTS - 1):
                break
            time.sleep(self._retry_after(last, attempt))
        raise_for_upstream(last, service="productlane")
        return last.json() if last.content else {}

    def _list(self, path: str, limit: Optional[int], cursor: Optional[str],
              extra: Optional[Dict[str, Any]] = None) -> Any:
        self._check_limit(limit)
        params: Dict[str, Any] = {"limit": limit, "cursor": cursor}
        params.update(extra or {})
        return self._request("GET", path, params=params)

    # --- pagination ---------------------------------------------------------

    def iterate(self, method: Any, *args: Any,
                max_pages: Optional[int] = None, **kwargs: Any) -> Iterator[Any]:
        """Déroule une liste paginée par curseur, page après page, et rend les LIGNES.

        Écrit une fois ce que chaque appelant réécrirait mal : la boucle s'arrête
        sur `page.has_more`, **pas** sur `data` vide — la doc éditeur prévient
        qu'une dernière page peut être vide « if it lined up », et s'arrêter là
        raterait le cas inverse (des lignes derrière un `has_more` vrai).

        `method` est une méthode de liste de ce client, passée telle quelle ::

            for fil in client.iterate(client.list_threads, status="open"):
                ...

        `max_pages` borne le déroulé — utile quand l'appelant sert un agent et
        doit tenir un budget de réponse.

        ⚠️ Ne pas passer `cursor` : c'est cette boucle qui le gère.
        """
        if "cursor" in kwargs:
            raise ValueError(
                "`iterate` gère le curseur lui-même — ne pas le passer.")
        pages = 0
        cursor: Optional[str] = None
        while True:
            payload = method(*args, cursor=cursor, **kwargs)
            if not isinstance(payload, dict):
                return
            for row in payload.get("data") or []:
                yield row
            page = payload.get("page") or {}
            cursor = page.get("cursor")
            pages += 1
            if not page.get("has_more") or not cursor:
                return
            if max_pages is not None and pages >= max_pages:
                return


__all__ = [
    "ProductlaneClient",
    "DEFAULT_LIMIT", "MIN_LIMIT", "MAX_LIMIT", "HTTP_TIMEOUT",
    "RETRY_STATUSES", "MAX_ATTEMPTS",
    "THREAD_STATUSES", "THREAD_TABS", "PAIN_LEVELS", "THREAD_ORIGINS",
    "THREAD_EXPANDS", "MESSAGE_ORDERS", "MESSAGE_TYPES", "MESSAGE_DIRECTIONS",
    "BLOCKED_SENDER_TYPES", "PROJECT_STATES", "ROADMAP_SORTS",
    "DOC_VISIBILITIES", "DOC_VISIBILITY_FILTERS", "DOC_KINDS",
    "DOC_KIND_FILTERS", "DRAFT_KINDS", "DRAFT_STATUSES", "ISSUE_PRIORITIES",
]
