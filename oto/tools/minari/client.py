"""Minari API client — prospection téléphonique : journal d'appels transcrits,
listes de contacts, champs personnalisés, analytics d'équipe.

Minari (minari.ai) est un composeur d'appels sortants : l'équipe compose, Minari
enregistre, transcrit, résume et détecte les objections. L'API publique v1
(`https://api.minari.ai/v1`) est en Bearer, la clé se crée dans
**Settings → API & webhook** et vaut pour TOUTE l'entreprise (pas par personne).

**Écrit sur contrat, PAS vérifié en live** (2026-08-31). Tout ici est dérivé de
l'OpenAPI 3.1 publié (`https://api.minari.ai/docs/openapi.json`) et du guide
d'usage LLM de l'éditeur. Aucune sonde n'a été passée contre un vrai compte : les
quatre points ci-dessous sont donc des lectures du contrat, pas des mesures — à
confirmer au premier compte branché.

**(1) L'enveloppe n'est PAS uniforme, et `data` n'est pas le tout.** Le contrat
annonce « toute réponse est enveloppée dans `data` », mais `has_more` / `next_url`
(pagination) et `period` (fenêtre analytique résolue) sont des **frères** de
`data`, pas des enfants. Un client qui déballerait `data` perdrait la pagination
en silence — donc **toutes** les méthodes ici rendent l'enveloppe ENTIÈRE, telle
quelle. Déballer est le travail de l'appelant, qui sait s'il veut la suite.

**(2) La portée des listes n'est pas celle des appels.** Les endpoints
listes/contacts ne voient QUE la source **import CSV** : un compte dont les
contacts viennent de HubSpot ou Salesforce a des listes bien réelles qu'ils ne
rendent jamais. Les endpoints appels et analytics, eux, couvrent **toutes** les
sources. Le piège est qu'un `GET /lists` vide se lit « pas de listes » alors qu'il
faut lire « pas de listes CSV » ; `analytics_lists()` est la vue tous-sources.

**(3) Il n'y a pas d'URL d'enregistrement dans une fiche d'appel.** `CallSummary`
porte `public_call_link` (la page partageable) mais aucun `recording_url` — ce
champ n'existe que dans la charge utile des webhooks. Le seul accès à l'audio est
`GET /calls/{id}/recording`, qui **streame un MP3**. D'où `call_recording_status()`
plus bas : il sonde la disponibilité sans jamais tirer le corps audio.

**(4) 60 requêtes/minute PAR ENTREPRISE**, pas par clé ni par utilisateur. Deux
automatisations qui tournent sous la même clé se partagent donc le même budget.
Le 429 remonte avec les secondes de réarmement lues dans `RateLimit-Reset`,
sinon l'appelant n'a aucun moyen de savoir combien attendre.

Ce module n'invente aucune capacité : Minari n'expose pas de déclenchement
d'appel, pas de modification de contact, pas de gestion d'utilisateurs. Ce qui
manque ici manque à l'API.

Requires: requests
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Sequence
from urllib.parse import quote, urlparse

import requests

from ...config import require_secret
from ..common import UpstreamHTTPError, raise_for_upstream

_HTTP_TIMEOUT = (10, 60)  # (connexion, lecture)
_BASE_URL = "https://api.minari.ai/v1"
#: Préfixe lu par la sonde d'enregistrement — assez pour reconnaître du
#: JSON, jamais assez pour tirer un MP3.
_PROBE_PREFIX_BYTES = 8192

#: Plafond de contacts, identique par requête et par liste (contrat Minari).
MAX_CONTACTS_PER_REQUEST = 1500
MAX_CONTACTS_PER_LIST = 1500

#: Les seuls seuils de conversation acceptés par les endpoints analytics.
CONVERSATION_THRESHOLDS = (0, 30, 60, 90, 120)

#: Fenêtres de comptage de `analytics_lists`.
LIST_PERIODS = ("day", "week", "month", "all")

#: Valeurs acceptées par le FILTRE `status` de `list_calls` — il en compte NEUF.
#: ⚠️ Asymétrie du contrat : le `status` d'une ligne d'appel ne prend que les huit
#: premières (`CALL_STATUSES_RETURNED`). `meeting-booked` est un critère de
#: recherche, pas un état rendu — côté ligne, c'est le booléen `meeting_booked`.
#: Recopier l'énumération de la RÉPONSE dans le filtre coûte la seule façon de
#: demander « les appels qui ont donné un rendez-vous » sans balayer le journal.
CALL_STATUSES = (
    "connected", "missed", "voicemail", "left-voicemail",
    "canceled", "busy", "failed", "no-answer", "meeting-booked",
)
CALL_STATUSES_RETURNED = CALL_STATUSES[:-1]

#: Un contact doit porter au moins un de ces champs, sinon Minari le rejette.
_CONTACT_IDENTIFYING_FIELDS = ("firstName", "lastName", "email")


def _id(value: Any) -> str:
    """Échappe un identifiant avant de le coller dans un chemin d'URL.

    Ces ids viennent d'un agent. Non échappé, un `call_id` contenant `?` ou `#`
    ajoute des paramètres à la requête ou tronque le chemin : le serveur répond
    alors à une AUTRE question que celle posée. `safe=""` laisse `/` s'échapper
    aussi — un id n'a jamais de segment.
    """
    return quote(str(value), safe="")


class MinariClient:
    """Client Minari (API publique v1), auth Bearer, clé au niveau entreprise."""

    def __init__(self, api_key: Optional[str] = None, *,
                 base_url: Optional[str] = None):
        """
        Args:
            api_key: clé API Minari (ou variable d'env `MINARI_API_KEY`), créée
                dans Settings → API & webhook. Elle porte les droits de
                l'ENTREPRISE entière, pas d'un utilisateur.
            base_url: surcharge de l'hôte, pour un test ou un environnement
                dédié. Défaut `https://api.minari.ai/v1`.
        """
        self.api_key = api_key or require_secret("MINARI_API_KEY")
        self.base_url = (base_url or _BASE_URL).rstrip("/")
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {self.api_key}"
        self.session.headers["Accept"] = "application/json"

    # --- transport ----------------------------------------------------------

    def _request(self, method: str, path: str, *,
                 params: Optional[Dict[str, Any]] = None,
                 json_body: Any = None) -> Any:
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        resp = self.session.request(
            method, f"{self.base_url}{path}",
            params=clean or None, json=json_body, timeout=_HTTP_TIMEOUT)
        self._raise(resp)
        return self._body(resp)

    @staticmethod
    def _body(resp: Any) -> Any:
        """Le JSON de la réponse — un corps illisible est une faute AMONT.

        `resp.json()` lève `json.JSONDecodeError`, sous-classe de `ValueError` :
        laissé tel quel, il se confond en aval avec les `ValueError` de validation
        de ce module, et l'appelant accuse alors les arguments de l'utilisateur
        d'une panne du serveur distant.
        """
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError as e:
            raise UpstreamHTTPError(
                resp.status_code,
                {"detail": f"réponse Minari illisible (JSON attendu) : {e}",
                 "body": resp.text[:500]},
                service="minari") from e

    def _raise(self, resp: Any) -> None:
        """Comme `raise_for_upstream`, mais un 429 emporte son délai d'attente.

        Sans `RateLimit-Reset` dans le message, un appelant qui prend un 429 ne
        peut que deviner combien de temps patienter — et devine mal.
        """
        if resp.status_code == 429:
            try:
                body = resp.json()
            except Exception:
                body = resp.text
            reset = resp.headers.get("RateLimit-Reset")
            detail = (f"limite de 60 requêtes/minute (par entreprise) atteinte ; "
                      f"réarmement dans {reset} s" if reset else
                      "limite de 60 requêtes/minute (par entreprise) atteinte")
            raise UpstreamHTTPError(429, {"detail": detail, "body": body},
                                    service="minari")
        raise_for_upstream(resp, service="minari")

    def _get(self, path: str, **params: Any) -> Any:
        return self._request("GET", path, params=params)

    def next_page(self, next_url: str) -> Any:
        """Suit le `next_url` d'une réponse paginée — c'est une URL ABSOLUE.

        L'URL est vérifiée contre l'hôte configuré avant d'être suivie : suivre
        une URL rendue par l'amont sans la valider transformerait une réponse
        contrôlée par un tiers en requête sortante arbitraire (SSRF), avec notre
        en-tête `Authorization` dessus.
        """
        parsed = urlparse(next_url)
        expected = urlparse(self.base_url)
        if (parsed.scheme, parsed.netloc) != (expected.scheme, expected.netloc):
            raise ValueError(
                f"`next_url` pointe hors de l'hôte configuré "
                f"({parsed.scheme}://{parsed.netloc} ≠ {self.base_url}) — non suivi.")
        # `allow_redirects=False` : sans lui la garde ci-dessus ne vaut rien —
        # requests suit les redirections par défaut, donc un `next_url` conforme
        # qui répond `302 → ailleurs` y porterait notre `Authorization`. Un
        # amont qui redirige son propre lien de pagination est déjà anormal.
        resp = self.session.get(next_url, timeout=_HTTP_TIMEOUT,
                                allow_redirects=False)
        if 300 <= resp.status_code < 400:
            raise ValueError(
                f"`next_url` redirige (HTTP {resp.status_code} vers "
                f"{resp.headers.get('Location')!r}) — non suivi : la cible d'une "
                "redirection n'est plus celle qu'on a validée.")
        self._raise(resp)
        return self._body(resp)

    # --- validation ---------------------------------------------------------

    @staticmethod
    def _check_contacts(contacts: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Refuse ici ce que Minari refuserait, mais en nommant le coupable.

        Un lot de 1500 contacts rejeté d'un bloc pour « au moins un de firstName,
        lastName ou email » n'apprend pas QUEL contact est en faute : le rang est
        la seule information utile pour corriger un import.
        """
        items = list(contacts or [])
        if not items:
            raise ValueError("`contacts` est vide — Minari exige au moins un contact.")
        if len(items) > MAX_CONTACTS_PER_REQUEST:
            raise ValueError(
                f"{len(items)} contacts pour un plafond de "
                f"{MAX_CONTACTS_PER_REQUEST} par requête et de "
                f"{MAX_CONTACTS_PER_LIST} par liste — découper l'import en lots.")
        for i, c in enumerate(items):
            if not isinstance(c, dict):
                raise ValueError(f"contact #{i} : attendu un objet, reçu {type(c).__name__}.")
            if not any(str(c.get(f) or "").strip() for f in _CONTACT_IDENTIFYING_FIELDS):
                raise ValueError(
                    f"contact #{i} : il faut au moins un de `firstName`, `lastName` "
                    f"ou `email` — Minari rejette un contact sans aucun des trois.")
        return items

    @staticmethod
    def _check_threshold(value: Optional[int]) -> Optional[int]:
        if value is None or value in CONVERSATION_THRESHOLDS:
            return value
        raise ValueError(
            f"`conversation_threshold` doit valoir l'un de "
            f"{', '.join(str(v) for v in CONVERSATION_THRESHOLDS)} (reçu {value}).")

    # --- appels -------------------------------------------------------------

    def list_calls(self, *,
                   start_date: Optional[str] = None,
                   end_date: Optional[str] = None,
                   user_id: Optional[Sequence[Any]] = None,
                   status: Optional[Sequence[str]] = None,
                   direction: Optional[str] = None,
                   min_duration: Optional[int] = None,
                   search: Optional[str] = None,
                   transcript_search: Optional[str] = None,
                   language: Optional[str] = None,
                   contact_id: Optional[int] = None,
                   list_id: Optional[Sequence[Any]] = None,
                   cursor: Optional[str] = None) -> Any:
        """GET /calls — les appels terminés, du plus récent au plus ancien.

        Page fixée à 50 ; suivre `next_url` via `next_page()`. `search` porte sur
        le contact (nom, société, numéro) et `transcript_search` sur ce qui a été
        DIT — sous-chaîne littérale, mots ET-és, jamais sémantique, et sans
        traduction : chercher un terme anglais ne trouve pas un appel français.
        Les deux exigent au moins 3 caractères.
        """
        return self._get(
            "/calls", start_date=start_date, end_date=end_date,
            user_id=list(user_id) if user_id else None,
            status=list(status) if status else None,
            direction=direction, min_duration=min_duration, search=search,
            transcript_search=transcript_search, language=language,
            contact_id=contact_id,
            list_id=list(list_id) if list_id else None, cursor=cursor)

    def get_call(self, call_id: str) -> Any:
        """GET /calls/{id} — la fiche complète : transcript intégral + objections
        détaillées (catégorie, résumé, réponse du commercial, issue)."""
        return self._get(f"/calls/{_id(call_id)}")

    def get_call_transcript(self, call_id: str) -> Any:
        """GET /calls/{id}/transcript — le transcript seul.

        `transcript` vaut `null` quand l'appel n'a pas abouti ou que la
        transcription est encore en cours : ce n'est pas une erreur.
        """
        return self._get(f"/calls/{_id(call_id)}/transcript")

    def call_recording_status(self, call_id: str) -> Dict[str, Any]:
        """Disponibilité de l'enregistrement, SANS tirer l'audio.

        `GET /calls/{id}/recording` streame un MP3 quand il existe, et rend du
        JSON (`recording_url: null`) sinon. Charger le corps pour découvrir
        lequel des deux coûterait le poids d'un appel entier à chaque sonde —
        on lit donc les en-têtes et on referme.

        Rend `{available, content_type, size_bytes, url}`. `url` est l'adresse du
        flux, à ouvrir avec le même Bearer ; pour un lien PARTAGEABLE sans clé,
        c'est `public_call_link` de la fiche d'appel qu'il faut.
        """
        url = f"{self.base_url}/calls/{_id(call_id)}/recording"
        # L'en-tête de session annonce `application/json` ; ici on attend d'abord
        # de l'audio. Sans cette surcharge, un serveur qui négocie le contenu
        # pourrait répondre 406 — ou servir une erreur JSON qu'on lirait comme
        # « pas d'enregistrement ».
        headers = {"Accept": "audio/mpeg, application/json;q=0.5"}
        with self.session.get(url, timeout=_HTTP_TIMEOUT, stream=True,
                              headers=headers) as resp:
            self._raise(resp)
            content_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip()
            size = resp.headers.get("Content-Length")
            size_bytes = int(size) if size and size.isdigit() else None
            if content_type.startswith("audio/"):
                return {"available": True, "content_type": content_type,
                        "size_bytes": size_bytes, "url": url}
            # Type inattendu : NE PAS appeler `resp.json()`, qui lirait le corps
            # ENTIER — sur un MP3 mal étiqueté (ou servi en `octet-stream`) c'est
            # tout l'audio tiré en mémoire pour une simple sonde. On lit un
            # préfixe borné et on ne conclut « pas d'enregistrement » que si ce
            # préfixe est effectivement du JSON.
            head = next(resp.iter_content(_PROBE_PREFIX_BYTES), b"") or b""
            try:
                detail = json.loads(head.decode("utf-8", "replace"))
            except ValueError:
                # Ni audio déclaré, ni JSON lisible : quelque chose EST servi.
                # Le dire disponible avec son type réel vaut mieux que nier un
                # enregistrement qui existe.
                return {"available": True, "content_type": content_type or None,
                        "size_bytes": size_bytes, "url": url,
                        "note": "type inattendu — vérifie `content_type`"}
            return {"available": False, "content_type": content_type or None,
                    "size_bytes": None, "url": None, "detail": detail}

    # --- équipe -------------------------------------------------------------

    def list_users(self) -> Any:
        """GET /users — les membres actifs (invitation acceptée). Leur `id` est
        ce qu'attend `assigned_to` à la création d'une liste."""
        return self._get("/users")

    # --- listes -------------------------------------------------------------

    def list_lists(self, *, cursor: Optional[str] = None) -> Any:
        """GET /lists — les listes de contacts, plus récentes d'abord.

        ⚠️ Source **import CSV uniquement** : les listes synchronisées depuis un
        CRM n'apparaissent pas ici. Pour la vue toutes sources, `analytics_lists()`.
        """
        return self._get("/lists", cursor=cursor)

    def get_list(self, list_id: str) -> Any:
        """GET /lists/{id} — la liste et TOUS ses contacts (jusqu'à 1500), avec
        leurs notes. Pas de pagination : la réponse peut être volumineuse."""
        return self._get(f"/lists/{_id(list_id)}")

    def create_list(self, *, name: str, assigned_to: int,
                    contacts: Sequence[Dict[str, Any]],
                    update_existing_contacts: bool = False) -> Any:
        """POST /lists — crée une liste et y importe des contacts.

        `assigned_to` = l'`id` d'un membre rendu par `list_users()`. Un contact
        déjà connu du compte est AJOUTÉ à la liste, jamais dupliqué ; ses champs
        stockés restent inchangés sauf `update_existing_contacts=True` (une
        valeur vide n'écrase jamais une donnée existante).
        """
        return self._request("POST", "/lists", json_body={
            "name": name,
            "assignedTo": assigned_to,
            "contacts": self._check_contacts(contacts),
            "updateExistingContacts": update_existing_contacts,
        })

    def delete_list(self, list_id: str) -> Any:
        """DELETE /lists/{id} — supprime la liste. Irréversible."""
        return self._request("DELETE", f"/lists/{_id(list_id)}")

    # --- contacts -----------------------------------------------------------

    def add_contacts(self, list_id: str, contacts: Sequence[Dict[str, Any]], *,
                     update_existing_contacts: bool = False) -> Any:
        """POST /lists/{id}/contacts — ajoute des contacts à une liste existante.

        ⚠️ Le dépassement du plafond de 1500 par liste ne fait PAS échouer la
        requête : les contacts en trop sont ignorés et comptés dans
        `skippedCount`. Un `skippedCount` non nul est donc à lire.
        """
        return self._request("POST", f"/lists/{_id(list_id)}/contacts", json_body={
            "contacts": self._check_contacts(contacts),
            "updateExistingContacts": update_existing_contacts,
        })

    def remove_contacts(self, list_id: str, contact_ids: Sequence[int]) -> Any:
        """DELETE /lists/{id}/contacts — retire des contacts de la liste.

        Minari refuse de vider une liste entièrement par ce chemin : pour cela
        c'est `delete_list()`.
        """
        ids = list(contact_ids or [])
        if not ids:
            raise ValueError("`contact_ids` est vide — rien à retirer.")
        return self._request("DELETE", f"/lists/{_id(list_id)}/contacts",
                             json_body={"contactIds": ids})

    # --- champs personnalisés -----------------------------------------------

    def list_custom_fields(self) -> Any:
        """GET /custom-fields — les champs personnalisés déclarés. Leur `id` est
        la clé à employer dans le `customFields` d'un contact importé."""
        return self._get("/custom-fields")

    def create_custom_field(self, *, field_id: str, label: str) -> Any:
        """POST /custom-fields — déclare un champ. `field_id` doit être unique
        (409 sinon) et devient la clé utilisable dans `customFields`."""
        return self._request("POST", "/custom-fields",
                             json_body={"id": field_id, "label": label})

    def delete_custom_field(self, field_id: str) -> Any:
        """DELETE /custom-fields — retire un champ des imports et de l'UI."""
        return self._request("DELETE", "/custom-fields",
                             json_body={"id": field_id})

    # --- analytics ----------------------------------------------------------

    def analytics_overview(self, *,
                           start_date: Optional[str] = None,
                           end_date: Optional[str] = None,
                           user_id: Optional[Sequence[Any]] = None,
                           list_id: Optional[Sequence[Any]] = None,
                           conversation_threshold: Optional[int] = None) -> Any:
        """GET /analytics/overview — les chiffres de l'entreprise sur une période.

        ⚠️ Sans `start_date`/`end_date`, la fenêtre est **aujourd'hui**, pas
        « tout ». Les deux dates vont ensemble. Les taux sont des pourcentages
        (0–100) et valent `null` quand leur dénominateur est nul. La réponse
        renvoie la fenêtre résolue dans `period`, frère de `data`.
        """
        return self._get(
            "/analytics/overview", start_date=start_date, end_date=end_date,
            user_id=list(user_id) if user_id else None,
            list_id=list(list_id) if list_id else None,
            conversation_threshold=self._check_threshold(conversation_threshold))

    def analytics_users(self, *,
                        start_date: Optional[str] = None,
                        end_date: Optional[str] = None,
                        user_id: Optional[Sequence[Any]] = None,
                        list_id: Optional[Sequence[Any]] = None,
                        conversation_threshold: Optional[int] = None) -> Any:
        """GET /analytics/users — les mêmes métriques, une ligne par membre.

        Même défaut « aujourd'hui » que l'overview. Les lignes se somment aux
        totaux de l'overview à filtres égaux.
        """
        return self._get(
            "/analytics/users", start_date=start_date, end_date=end_date,
            user_id=list(user_id) if user_id else None,
            list_id=list(list_id) if list_id else None,
            conversation_threshold=self._check_threshold(conversation_threshold))

    def analytics_objections(self, *,
                             start_date: Optional[str] = None,
                             end_date: Optional[str] = None,
                             user_id: Optional[Sequence[Any]] = None,
                             list_id: Optional[Sequence[Any]] = None) -> Any:
        """GET /analytics/objections — les objections les plus rencontrées, par
        catégorie, avec la part engagée / passée / ayant mis fin à l'appel.

        ⚠️ Fenêtre par défaut : les **7 derniers jours** — pas « aujourd'hui »
        comme l'overview. Pas de pagination, une ligne par catégorie.
        """
        return self._get(
            "/analytics/objections", start_date=start_date, end_date=end_date,
            user_id=list(user_id) if user_id else None,
            list_id=list(list_id) if list_id else None)

    def analytics_lists(self, *, period: str, call_limit: int,
                        user_id: Optional[Sequence[Any]] = None,
                        list_id: Optional[Sequence[Any]] = None,
                        cursor: Optional[str] = None) -> Any:
        """GET /analytics/lists — l'avancement des listes, **toutes sources**.

        `period` et `call_limit` sont REQUIS parce qu'ils définissent ce que
        « terminé » veut dire : `period` est la fenêtre de comptage des appels,
        `call_limit` le nombre de tentatives après lequel un contact jamais
        joint est considéré comme épuisé. Deux réponses différentes à la même
        question sont donc deux définitions différentes, pas une incohérence.

        Page fixée à 10 ici (et non 50) ; suivre `next_url` via `next_page()`.
        """
        if period not in LIST_PERIODS:
            raise ValueError(
                f"`period` doit valoir l'un de {', '.join(LIST_PERIODS)} (reçu {period!r}).")
        if not 1 <= int(call_limit) <= 10:
            raise ValueError(
                f"`call_limit` doit être entre 1 et 10 (reçu {call_limit}).")
        return self._get(
            "/analytics/lists", period=period, call_limit=int(call_limit),
            user_id=list(user_id) if user_id else None,
            list_id=list(list_id) if list_id else None, cursor=cursor)
