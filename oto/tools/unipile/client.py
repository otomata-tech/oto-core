"""Unipile API client — hosted LinkedIn search / scrape / messaging (API v2).

Unipile maintient la session LinkedIn côté serveur (vrai Chrome + proxy
résidentiel), ce qui contourne les deux contraintes du browser local : empreinte
TLS et isolation de session (le cookie ne vit pas sur notre IP datacenter, donc
n'expose ni ne déconnecte la session de l'utilisateur). Cf. oto-mcp#5.

Requires: requests

Secrets (résolus via oto.config) :
- UNIPILE_API_KEY            (requis) — clé X-API-KEY du compte Unipile
- UNIPILE_DSN                (def. api.unipile.com) — host de l'instance
- UNIPILE_LINKEDIN_ACCOUNT_ID (optionnel) — sinon, 1er compte LINKEDIN connecté

Spécificités API v2 (par rapport à l'ancienne API v1, retirée) :
- **base** : `https://{dsn}/v2`.
- **`account_id` dans le PATH** (`/v2/{account_id}/…`), plus en query param — donc
  ne fuite plus dans les query strings, mais peut apparaître dans une URL d'erreur
  → on le **caviarde** dans les messages (`_sanitize`, feedback oto #178).
- **enveloppe de liste** `{data, total_count, next_cursor}`. On **normalise** chaque
  réponse de liste vers `items`/`cursor` EN PLUS de garder `data`/`next_cursor` →
  l'aval oto-mcp (feed sync, wrappers, attendus de l'agent) reste stable.
- **surface éclatée** : search people/companies séparés + par produit
  (classic/recruiter/sales-navigator) ; invitations = `users/me/relation-requests` ;
  attendees d'un fil = `participants` ; réactions de message sous le chat ;
  solde InMail = `inmail-credits`.

Fixes feedback fold-in (au-delà de l'API brute) :
- **garde anti-mismatch** identifier↔réponse sur `get_profile`/`get_company`
  (feedback #144-149/#153 : sous concurrence, l'API a rendu le profil d'un AUTRE
  membre / un CompanyProfile à la place). On vérifie que l'objet rendu correspond
  bien au type ET à l'identifiant demandés, sinon `UnipileError` **actionnable et
  retryable** — jamais de donnée fausse renvoyée en silence.
- **erreurs réseau propres** : les exceptions `requests` (DNS/timeout) sont mappées
  en `UnipileError` stable au lieu de fuiter `net::ERR_NAME_NOT_RESOLVED` (#177).
- **résolution de slug tolérante** sur `get_company` (#176) : un nom de marque
  passé comme slug (`mooniz`) qui tombe en 404 est retenté via une recherche
  société → `public_identifier` canonique (`mooniz1`) ; échec → 404 propre
  enrichi des candidats proches, jamais l'erreur brute.

Structure du package (découpage 2026-08-27, surface publique INCHANGÉE) :
`client.py` porte la classe `UnipileClient` — construction, transport et
normalisation — et compose les familles d'appels de `_api/` (comptes, recherche,
profils, messagerie, réseau, contenu, premium). Les constantes/helpers vivent
dans `const.py`, les erreurs dans `errors.py`, le parsing du feed dans `feed.py`
— tous **réexportés ici**, car `oto.tools.unipile.client` est le chemin d'import
que le backend et les tests utilisent.
"""

from __future__ import annotations

import logging
from typing import Any, Optional
from urllib.parse import quote

import requests

from ...config import get_secret, require_secret
from ._api import (
    _AccountsMixin,
    _ContentMixin,
    _MessagingMixin,
    _NetworkMixin,
    _PremiumMixin,
    _ProfilesMixin,
    _SearchMixin,
)
from .const import (
    DEFAULT_DSN,
    FEED_QUERY_ID,
    _API_PREFIX,
    _DEFAULT_PROVIDER,
    _INBOX_PROVIDERS,
    _REQUEST_TIMEOUT,
    _SCRAPE_TIMEOUT,
    _URL_SEARCH_TIMEOUT,
    _sections_param,
    _slug_from_company_url,
    cursor_with_limit,
)
from .errors import (
    UnipileError,
    UnipileRateLimited,
    _RETRY_RE,
    _parse_retry_after,
)
from .feed import (
    _CAMEL_SPLIT,
    _CONTENT_ALIASES,
    _CONTENT_LABEL_KEYS,
    _CONTENT_LABEL_MAX,
    _CONTENT_PRIORITY,
    _activity_urn_from,
    _annotated_entity,
    _comment_authors,
    _content_facets,
    _content_key_to_type,
    _content_label,
    _deep_get,
    _extract_activity,
    _feed_context,
    _is_promo,
    _map_feed_item,
    _posted_at_from_activity,
    _social_counts,
    _text_of,
    _unpack_cursor,
    parse_feed,
)

logger = logging.getLogger(__name__)

# Surface figée de `oto.tools.unipile.client` : ce module reste LE point d'import
# du connecteur. Tout ce qui y était importable avant le découpage l'est encore —
# le backend prend `UnipileError`/`UnipileRateLimited` ici, et la suite de tests
# `cursor_with_limit`, `parse_feed`, `_parse_retry_after`, `_activity_urn_from`…
# Les noms préfixés d'un `_` sont listés parce qu'ils sont IMPORTÉS AILLEURS, pas
# parce qu'ils seraient publics : `tests/test_unipile_surface_frozen.py` verrouille
# cette liste.
__all__ = [
    "DEFAULT_DSN",
    "FEED_QUERY_ID",
    "UnipileClient",
    "UnipileError",
    "UnipileRateLimited",
    "cursor_with_limit",
    "parse_feed",
    "_API_PREFIX",
    "_CAMEL_SPLIT",
    "_CONTENT_ALIASES",
    "_CONTENT_LABEL_KEYS",
    "_CONTENT_LABEL_MAX",
    "_CONTENT_PRIORITY",
    "_DEFAULT_PROVIDER",
    "_INBOX_PROVIDERS",
    "_REQUEST_TIMEOUT",
    "_RETRY_RE",
    "_SCRAPE_TIMEOUT",
    "_URL_SEARCH_TIMEOUT",
    "_activity_urn_from",
    "_annotated_entity",
    "_comment_authors",
    "_content_facets",
    "_content_key_to_type",
    "_content_label",
    "_deep_get",
    "_extract_activity",
    "_feed_context",
    "_is_promo",
    "_map_feed_item",
    "_parse_retry_after",
    "_posted_at_from_activity",
    "_sections_param",
    "_slug_from_company_url",
    "_social_counts",
    "_text_of",
    "_unpack_cursor",
]


class UnipileClient(
    _AccountsMixin,
    _SearchMixin,
    _ProfilesMixin,
    _MessagingMixin,
    _NetworkMixin,
    _ContentMixin,
    _PremiumMixin,
):
    """Client Unipile API v2 — hosted LinkedIn (et autres IM)."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        dsn: Optional[str] = None,
        account_id: Optional[str] = None,
        provider: Optional[str] = None,
    ):
        self.api_key = api_key or require_secret("UNIPILE_API_KEY")
        self.dsn = dsn or get_secret("UNIPILE_DSN", DEFAULT_DSN)
        self.base_url = f"https://{self.dsn}/v2"
        self._account_id = account_id or get_secret("UNIPILE_LINKEDIN_ACCOUNT_ID")
        # Canal du compte opéré (LINKEDIN, WHATSAPP, …). Sert la forme d'endpoint de
        # messagerie (cf. `_INBOX_PROVIDERS`) ; None = supposé LinkedIn (compat).
        self.provider = (provider or "").strip().upper() or None
        self.session = requests.Session()
        self.session.headers.update(
            {"X-API-KEY": self.api_key, "accept": "application/json"}
        )

    # ---- transport -------------------------------------------------------

    def _sanitize(self, msg: str) -> str:
        """Caviarde l'account_id dans un message d'erreur (il vit dans le path v2 →
        remonterait sinon dans une URL 404, feedback #178)."""
        acct = self._account_id
        if acct and isinstance(msg, str):
            return msg.replace(acct, "<account>")
        return msg

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[dict] = None,
        json: Optional[dict] = None,
        timeout: Optional[tuple] = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        try:
            resp = self.session.request(
                method, url, params=params, json=json,
                timeout=timeout or _REQUEST_TIMEOUT)
        except requests.RequestException as e:
            # DNS/timeout/reset : erreur stable au lieu de fuiter net::ERR_* (#177).
            raise UnipileError(
                self._sanitize(f"Unipile: erreur réseau ({type(e).__name__}).")
            ) from e
        if resp.status_code >= 400:
            try:
                body = resp.json()
                msg = (body.get("detail") or body.get("message")
                       or body.get("title") or resp.text)
            except (ValueError, AttributeError):
                msg = resp.text or f"{resp.status_code} {resp.reason}"
            full = self._sanitize(f"Unipile {resp.status_code}: {msg}")
            # 429 = quota amont (LinkedIn cappe fiches société/profil ~100/12h par
            # compte) → type dédié + délai parsé, l'appelant STOPPE (cf. UnipileRateLimited).
            if resp.status_code == 429:
                raise UnipileRateLimited(full, retry_after=_parse_retry_after(msg))
            raise UnipileError(full, status_code=resp.status_code)
        if not resp.text:
            return None
        return resp.json()

    def _acct(self, sub_path: str) -> str:
        """Préfixe un sous-chemin par `/{account_id}` (path param v2)."""
        return f"/{quote(self.account_id(), safe='')}{sub_path}"

    def uses_inboxes(self) -> bool:
        """Le provider de ce compte range-t-il sa messagerie par inbox ? (cf.
        `_INBOX_PROVIDERS`). Provider non déclaré → supposé LinkedIn."""
        return (self.provider or _DEFAULT_PROVIDER) in _INBOX_PROVIDERS

    def _by_shape(self, inbox_call, plain_call, what: str) -> Any:
        """Appelle la forme d'endpoint DÉCLARÉE pour ce provider, et bascule sur
        l'autre si Unipile répond **501**.

        Le 501 d'Unipile n'est pas une panne : c'est l'amont qui NOMME la forme
        attendue (« Use List inbox Chats endpoint », « Use Start a Chat in the given
        inbox endpoint for this provider », et le symétrique pour un provider sans
        inbox). Le rattrapage vaut donc dans les deux sens et ne masque rien
        d'autre — tout autre statut remonte tel quel, et la bascule est
        JOURNALISÉE : si Unipile reclasse un provider, ça se lit dans les logs au
        lieu de casser un canal en silence, comme le 2026-07-06 côté LinkedIn puis
        ce jour-là côté WhatsApp. L'identité (`account_id`) est dans le path des
        deux formes : rien ne bascule ici que la ROUTE."""
        inbox_first = self.uses_inboxes()
        first, second = ((inbox_call, plain_call) if inbox_first
                         else (plain_call, inbox_call))
        try:
            return first()
        except UnipileError as e:
            if e.status_code != 501:
                raise
            logger.warning(
                "unipile %s: 501 sur la forme %s pour provider=%s — bascule sur "
                "la forme %s. Si ça se répète, `_INBOX_PROVIDERS` a dérivé du "
                "modèle Unipile.",
                what, "inbox" if inbox_first else "plate",
                self.provider or f"{_DEFAULT_PROVIDER} (supposé)",
                "plate" if inbox_first else "inbox")
            return second()

    @staticmethod
    def _norm(data: Any) -> Any:
        """Normalise une enveloppe de liste v2 `{data, next_cursor, total_count}`
        vers la forme `items`/`cursor` attendue par l'aval SANS perdre les champs
        natifs. No-op si `data` n'est pas une enveloppe de liste."""
        if not isinstance(data, dict):
            return data
        if "data" in data and isinstance(data.get("data"), list):
            data.setdefault("items", data["data"])
        if "next_cursor" in data:
            data.setdefault("cursor", data.get("next_cursor"))
        return data
