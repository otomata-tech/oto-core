"""Leexi API client — intelligence conversationnelle (appels, transcripts, notes).

API v1 (`https://public-api.leexi.ai/v1`, doc https://docs.public-api.leexi.ai),
auth **HTTP Basic** `base64(KEY_ID:KEY_SECRET)`. Une méthode = un endpoint ; les
corps et les réponses passent tels quels, le client n'invente aucune sémantique.
Chemins, verbes, paramètres et scopes relevés page par page dans la référence
éditeur (OpenAPI embarqué à chaque page) le 2026-09-02.

Ce module porte la **construction et le transport**, et compose les familles
d'appels de `_api/` (utilisateurs, équipes, appels, notes, réunions). Les
constantes vivent dans `const.py` et sont réexportées ici : le backend épingle
oto-core par tag et n'importe que `oto.tools.leexi.client`.

Quatre choses conditionnent l'appelant, et aucune ne se devine :

- ⚠️ **Les paramètres de liste multi-valués s'écrivent `nom[]=a&nom[]=b`** (Rails).
  C'est LE piège du connecteur : `requests` sérialise `{"owner_uuid": ["a", "b"]}`
  en `owner_uuid=a&owner_uuid=b`, que Rails lit comme un SCALAIRE et réduit à la
  DERNIÈRE valeur — le filtre part, l'amont répond 200, et la réponse est celle
  d'un filtre sur `b` seul. Un filtre silencieusement rétréci est pire qu'un refus :
  l'appelant croit avoir listé les appels de deux propriétaires. `ARRAY_PARAMS`
  nomme les paramètres concernés et `_encode_params` leur pose le suffixe, une
  fois, au transport. La doc éditeur les écrit tous avec leurs crochets
  (« `source_id[]=abc&source_id[]=xyz` »).

- **Deux portées, à ne pas confondre.** La *call access scope* est attachée à la
  clé (toute l'entreprise / l'accès d'un utilisateur / des règles d'accès) et
  décide quels APPELS la clé voit : hors périmètre, un appel n'est pas listé et
  répond **404** en direct — donc un 404 sur `get_call` ne veut pas dire « n'existe
  pas ». Les *permission scopes* (`read_calls`, `write_users`…) décident quels
  ENDPOINTS la clé atteint : sans le scope, c'est un **403**. Les deux se
  configurent côté admin Leexi, jamais par cette API.

- ⚠️ **Une clé neuve ne porte QUE `read_calls`.** Tout le reste — et nommément
  `write_users` / `write_teams`, **qui engagent les licences facturées** — doit
  être accordé explicitement par un admin. D'où le choix de sonde : `probe()`
  interroge `/calls`, seul appel qu'une clé par défaut peut honorer. Sonder
  `/users` ferait passer une clé saine pour une clé morte (403 ≠ 401).

- **Pagination `page`/`items`, `items` plafonne à 100** (défaut 10). `_check_items`
  refuse localement hors bornes plutôt que de laisser partir un 400.

Les écritures de licence (`create_user`, `update_user`, `deactivate_user`) et de
structure (`create_team`, `update_team`, `delete_team`) sont exposées : elles sont
au périmètre décidé pour ce connecteur. Le garde-fou n'est pas ici — il est le
scope de la clé, qu'un admin Leexi accorde ou non. Ce client ne peut pas
contourner ce cran, et n'essaie pas.

⚠️ **Rien n'est jamais supprimé pour de bon côté utilisateur** : `deactivate_user`
est un `DELETE /users/{uuid}` qui *désactive* (les appels et l'historique restent,
les sessions tombent, la licence se libère). Le nom de la méthode dit l'effet réel,
pas le verbe HTTP.

**Cycle d'un appel importé** : `presign_recording_url(extension)` → PUT du fichier
sur l'URL rendue, **avec les en-têtes rendus** (`upload_recording` le fait) → puis
`create_call(recording_s3_key=…)`. Le fichier téléversé expire au bout de 3 jours
s'il ne sert pas à créer un appel. La création est **asynchrone** (quelques minutes)
et les complétions de prompt (résumé, chapitrage) arrivent APRÈS.

Limites d'usage amont : 50 requêtes/minute, et **10/minute pour la création
d'appel**. Le 429 est retenté en respectant `Retry-After` — **en lecture seule**
(cf. `_request` : l'API n'offre aucune clé d'idempotence).

Requires: requests
"""
from __future__ import annotations

import base64
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests

from ...config import require_secret
from ..common import raise_for_upstream
from ._api import (_CallsMixin, _MeetingsMixin, _NotesMixin, _TeamsMixin,
                   _UsersMixin)
from .const import (ARRAY_PARAMS, CALL_DATE_FILTERS, CALL_ORDERS, DEFAULT_ITEMS,
                    HTTP_TIMEOUT, MAX_ATTEMPTS, MAX_ITEMS, MEETING_DATE_FILTERS,
                    MEETING_ORDERS, MEETING_ORIGINS, MIN_ITEMS, RETRY_STATUSES)


def basic_signature(key_id: str, key_secret: str) -> str:
    """`base64(KEY_ID:KEY_SECRET)` — la valeur qui suit « Basic ».

    Écrite ici pour qu'un appelant (une sonde de connexion, un test) puisse la
    fabriquer sans recopier l'encodage, et pour qu'il n'en existe qu'une version.
    """
    raw = f"{key_id}:{key_secret}".encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


class LeexiClient(
    _UsersMixin,
    _TeamsMixin,
    _CallsMixin,
    _NotesMixin,
    _MeetingsMixin,
):
    """Client Leexi v1 (https://public-api.leexi.ai/v1), auth Basic KEY_ID:KEY_SECRET."""

    BASE_URL = "https://public-api.leexi.ai/v1"

    #: L'appel le moins exigeant de l'API : `read_calls` est le SEUL scope d'une
    #: clé neuve. Toute sonde d'authentification passe par là — `/users` exigerait
    #: `read_users`, qu'un admin n'a pas forcément accordé, et son 403 se lirait à
    #: tort comme « la clé est mauvaise ».
    PROBE_PATH = "/calls"

    def __init__(self, key_id: Optional[str] = None,
                 key_secret: Optional[str] = None):
        """
        Args:
            key_id: identifiant de clé Leexi (ou env `LEEXI_KEY_ID`).
            key_secret: secret de clé Leexi (ou env `LEEXI_KEY_SECRET`).

        Les deux se génèrent dans Leexi → Settings → Company Settings → API Keys
        (compte admin requis).
        """
        self.key_id = key_id or require_secret("LEEXI_KEY_ID")
        self.key_secret = key_secret or require_secret("LEEXI_KEY_SECRET")
        self.session = requests.Session()
        # Signature en HEADER uniquement (jamais en query string : elle finirait
        # dans l'URL, donc dans le message de toute exception, les logs et Sentry).
        self.session.headers.update({
            "Authorization": f"Basic {basic_signature(self.key_id, self.key_secret)}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        })

    # --- transport ----------------------------------------------------------

    @staticmethod
    def _check_items(items: Optional[int]) -> None:
        """`items` hors [1, 100] est refusé ICI. L'API rendrait un 400 ; le dire
        localement nomme la borne réelle, que personne ne devine."""
        if items is None:
            return
        if not isinstance(items, int) or isinstance(items, bool):
            raise ValueError("`items` doit être un entier.")
        if not (MIN_ITEMS <= items <= MAX_ITEMS):
            raise ValueError(
                f"`items` doit être entre {MIN_ITEMS} et {MAX_ITEMS} "
                f"(plafond de l'API Leexi) ; reçu {items}. "
                "Au-delà, paginer avec `page`.")

    @staticmethod
    def _check_choice(name: str, value: Optional[str],
                      allowed: Iterable[str]) -> None:
        """Refuse localement une valeur hors énumération, en NOMMANT les valides —
        l'amont rend un 400 qui, lui, ne les liste pas."""
        if value is None:
            return
        allowed = tuple(allowed)
        if value not in allowed:
            raise ValueError(
                f"`{name}` invalide : {value!r}. Valeurs acceptées : "
                + ", ".join(repr(a) for a in allowed))

    @staticmethod
    def _encode_params(params: Optional[Dict[str, Any]]) -> List[Tuple[str, Any]]:
        """Params → liste de paires, à plat, prête pour `requests`.

        Trois règles, toutes imposées par l'amont (Rails) :

        - `None` est retiré (un paramètre absent ≠ un paramètre vide) ;
        - une valeur d'`ARRAY_PARAMS` est répétée sous `nom[]` — c'est la
          correction décrite en tête de module, et la raison d'être de cette
          fonction ;
        - un booléen part en `true`/`false` (requests écrirait `True`, que le
          serveur ne lit pas comme un booléen).

        Une liste passée à un paramètre SCALAIRE est refusée net : la laisser
        filer produirait exactement le rétrécissement silencieux qu'on corrige.
        """
        out: List[Tuple[str, Any]] = []
        for key, value in (params or {}).items():
            if value is None:
                continue
            if key in ARRAY_PARAMS:
                values = value if isinstance(value, (list, tuple, set)) else [value]
                out.extend((f"{key}[]", v) for v in values if v is not None)
            elif isinstance(value, bool):
                out.append((key, "true" if value else "false"))
            elif isinstance(value, (list, tuple, set)):
                raise ValueError(
                    f"`{key}` n'accepte pas plusieurs valeurs. Multi-valués : "
                    + ", ".join(sorted(ARRAY_PARAMS)))
            else:
                out.append((key, value))
        return out

    @staticmethod
    def _retry_after(resp: Any, attempt: int) -> float:
        """Délai avant re-tentative : `Retry-After` s'il est là (l'amont sait mieux
        que nous — 50 req/min, 10/min sur la création), sinon backoff exponentiel."""
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
        # d'idempotence, donc rejouer un POST créerait un doublon — un appel de
        # plus, ou un utilisateur facturé de plus. Une écriture qui prend un 429
        # remonte telle quelle, à l'appelant de décider.
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
        raise_for_upstream(last, service="leexi")
        return last.json() if last.content else {}

    def _list(self, path: str, page: Optional[int], items: Optional[int],
              extra: Optional[Dict[str, Any]] = None) -> Any:
        self._check_items(items)
        params: Dict[str, Any] = {"page": page, "items": items}
        params.update(extra or {})
        return self._request("GET", path, params=params)

    # --- sonde --------------------------------------------------------------

    def probe(self) -> Dict[str, Any]:
        """Vérifie que la clé authentifie, au coût d'un seul appel.

        `GET /calls?items=1`. Un **401** dit que la paire KEY_ID/KEY_SECRET est
        mauvaise ; un **402** que l'abonnement Leexi est inactif ; un **403** que
        la clé n'a même pas `read_calls` (elle existe, mais un admin le lui a
        retiré). Une liste vide n'est PAS un échec : c'est une clé dont la *call
        access scope* ne couvre aucun appel, ce qui est un réglage valide.
        """
        return self._request("GET", self.PROBE_PATH, params={"items": 1})


__all__ = [
    "LeexiClient", "basic_signature",
    "MIN_ITEMS", "MAX_ITEMS", "DEFAULT_ITEMS", "ARRAY_PARAMS",
    "HTTP_TIMEOUT", "RETRY_STATUSES", "MAX_ATTEMPTS",
    "CALL_ORDERS", "CALL_DATE_FILTERS",
    "MEETING_ORDERS", "MEETING_DATE_FILTERS", "MEETING_ORIGINS",
]
