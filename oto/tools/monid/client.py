"""Monid — passerelle payante vers les endpoints de données de nombreux fournisseurs.

Monid (monid.ai) place derrière UNE clé d'API quelque 2 000 endpoints d'environ 70
fournisseurs (recherche web, scraping, enrichissement de contacts, réseaux
sociaux…), **facturés à l'appel sur un portefeuille prépayé** du workspace. Auth
`Authorization: Bearer <clé>` ; la clé est liée à son workspace.

**Écrit sur le contrat OpenAPI `0.1.0`** publié par Monid (`https://api.monid.ai`),
**pas encore sondé contre un vrai compte** : ce qui suit se lit dans le contrat.

Le parcours : `discover(q)` (recherche sémantique, fiches `{items, total}`) →
`inspect(provider, endpoint)` (seul endroit où se lisent le **schéma d'entrée** et
le **prix courant**) → `run(…)` (lance, et débite) → `get_run` / `wait_for_run` /
`list_runs` / `stop_run`. `wallet_balance()` dit ce qui reste.

⚠️ **`POST /v1/run` ne se lit pas au code HTTP.** Son statut REFLÈTE celui du
fournisseur : un run `COMPLETED` dont le fournisseur a répondu 404 revient en 404,
un délai dépassé en 408 (`TIMED_OUT`), le 402 d'un fournisseur en 502 — chaque fois
avec le run ENTIER dans le corps. C'est la FORME du corps qui tranche : `runId` +
`status` = un run, rendu tel quel ; l'enveloppe `{code, message}` = un refus de
Monid, levé en `MonidHTTPError`. Un 402 de Monid parle TOUJOURS du portefeuille.

⚠️ **Statut du run ≠ statut du fournisseur.** `COMPLETED` = « le fournisseur a
répondu », quoi qu'il ait répondu (`providerResponse.httpStatus`) ; `FAILED` = panne
côté Monid ; `BLOCKED` (rendu en 200) = un plafond du workspace (budget, nombre de
runs) a refusé le run avant exécution. Terminaux : `COMPLETED`, `FAILED`, `BLOCKED`,
`STOPPED`, `TIMED_OUT` — sans les deux derniers, une attente tournerait jusqu'à sa borne.

⚠️ **Un run n'est JAMAIS re-tenté**, un arrêt non plus : pas de clé d'idempotence,
et rejouer un lancement perdu en vol peut payer deux fois. Quand la requête a pu
partir sans réponse exploitable (délai de lecture, connexion rompue ou jamais
établie, corps illisible, 5xx), l'issue est INCONNUE : l'erreur porte
`may_have_run=True`, et c'est la liste des runs qu'il faut relire avant toute
nouvelle tentative. Seules les lectures (`GET`) sont re-tentées, sur 429/5xx ; un
`Retry-After` de plus de 15 s n'est pas dormi, il remonte dans `retry_after` ; un
503 du portefeuille SANS `Retry-After` (en échec, ou pas encore créé) remonte aussitôt.

⚠️ **L'entrée d'un run a trois parties** — `body`, `queryParams`, `pathParams` —
là où le schéma d'`inspect` les place, jamais à plat. `endpoint` (qui commence par
`/`) et `provider` (qui peut porter des points) passent tels que `discover` les rend.

**Ce qui est facturé se LIT, jamais ne se recalcule** (`run_cost_usd`) : `cost.value`
en dollars sur un run relu, `billing.reportedCost` en unités entières sur la réponse
du lancement, absent tant que le run n'est pas réglé ; `price` = le prix catalogue.

**Attendre est borné** : `wait_for_run` s'arrête à `max_wait_s` (plafond 300 s) et
rend le DERNIER état lu, terminal ou non — jamais de sommeil au-delà de l'échéance.
**Redirections jamais suivies** : une 3xx emporterait l'en-tête d'authentification ;
elle est refusée, sauf au lancement quand son corps est un run (statut recopié).

**Délibérément absents** : budgets et plafonds de runs, ressources, gestion des clés
d'API, recharge et historique du portefeuille, la route dépréciée `/v1/discover`, le
registre public `/public/v1/*`, l'en-tête de workspace (inutile avec une clé), et
`X-Monid-Client`, non envoyé : le contrat rend les `hints` selon le client déclaré
(commande pour cli/api/mcp, cible structurée pour web, omis si inconnu) ; sans cet
en-tête, la doc Monid les montre en commande curl — non vérifié, le client ne s'y fie pas.

Requires: requests
"""
from __future__ import annotations

import math
import re
import time
from typing import Any, Dict, Optional
from urllib.parse import quote

import requests

from ...config import require_secret
from ..common import UpstreamHTTPError

SERVICE = "monid"
DEFAULT_BASE_URL = "https://api.monid.ai"
_HTTP_TIMEOUT = (10, 30)  # (connexion, lecture) — aucune attente illimitée
RUN_READ_TIMEOUT = 60  # lecture du lancement : un fournisseur synchrone garde la connexion
RUN_STATUSES = ("READY", "RUNNING", "STOPPING", "COMPLETED", "FAILED", "BLOCKED",
                "STOPPED", "TIMED_OUT")
TERMINAL_STATUSES = frozenset({"COMPLETED", "FAILED", "BLOCKED", "STOPPED", "TIMED_OUT"})
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})  # lectures seulement
_MAX_ATTEMPTS = 3
_RETRY_AFTER_MAX = 15  # au-delà, le délai remonte à l'appelant au lieu d'être dormi
_MAX_WAIT_S = 300      # plafond de `wait_for_run`, quoi que demande l'appelant

_CATEGORY_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")  # en `fullmatch` : `$` admet un `\n` final
_ERROR_CODES = frozenset({  # registre `ApiErrorCode` du contrat 0.1.0 ; hors registre = absent
    "X402_WORKSPACE_RESOURCES", "X402_ACCRUING_COST", "X402_EXCEEDS_SETTLEMENT_WINDOW",
    "X402_VALIDITY_WINDOW_TOO_SHORT"})
_COST_DIVISORS = {"MICRO_DOLLAR": 1_000_000, "CENT": 100, "DOLLAR": 1}
_NOT_JSON = object()


class MonidHTTPError(UpstreamHTTPError):
    """Refus de Monid. Garde `.status_code` / `.body` (enveloppe parsée ou texte).

    `request_id` = l'en-tête `x-request-id` ; `retry_after` = le `Retry-After` en
    secondes (429 ; 503 d'un portefeuille en création — sans lui, un 503 de portefeuille
    en échec ou inexistant remonte sans nouvelle tentative). `may_have_run` n'est vrai
    que sur un 5xx du lancement d'un run : rien ne dit que le run n'a pas été créé.
    """

    may_have_run: bool = False

    def __init__(self, status_code: int, body: Any = None, *,
                 request_id: Optional[str] = None, retry_after: Optional[float] = None):
        super().__init__(status_code, body, service=SERVICE)
        self.request_id = request_id
        self.retry_after = retry_after
        detail = self.upstream_message
        if detail is None:
            detail = body if isinstance(body, (dict, list)) else _excerpt(body)
        # Le message du parent recopie le corps entier, page HTML d'intermédiaire comprise.
        self.args = (f"{SERVICE} HTTP {status_code}: {detail}",)

    @property
    def error_code(self) -> Optional[str]:
        """`errorCode` de l'enveloppe s'il est au registre du contrat ; une valeur hors
        registre vaut absence (le contrat l'exige) et reste lisible dans `.body`."""
        code = self.body.get("errorCode") if isinstance(self.body, dict) else None
        return code if isinstance(code, str) and code in _ERROR_CODES else None

    @property
    def upstream_message(self) -> Optional[str]:
        """Le message humain de Monid : `message`, sinon `error.message`."""
        if not isinstance(self.body, dict):
            return None
        message = self.body.get("message")
        if not (isinstance(message, str) and message.strip()):
            nested = self.body.get("error")
            message = nested.get("message") if isinstance(nested, dict) else None
        return message if isinstance(message, str) and message.strip() else None


class MonidProtocolError(RuntimeError):
    """Réponse inexploitable (redirection, 2xx non JSON, lecture perdue sur un run…).

    Défaut de transport ou de configuration, pas un refus métier. `may_have_run` : la
    requête d'un run a pu partir sans résultat lisible — le run peut exister, et être facturé.
    """

    def __init__(self, message: str, *, request_id: Optional[str] = None,
                 may_have_run: bool = False):
        super().__init__(message)
        self.request_id = request_id
        self.may_have_run = may_have_run


def is_terminal(run: dict) -> bool:
    """Le run est-il dans un état final ? Comparaison sensible à la casse ; un
    `status` qui n'est pas une chaîne n'est pas final (et ne lève pas)."""
    status = run.get("status") if isinstance(run, dict) else None
    return isinstance(status, str) and status in TERMINAL_STATUSES


def run_cost_usd(run: dict) -> Optional[float]:
    """Ce que le run a coûté, en dollars — LU dans la réponse, jamais recalculé.

    `cost.value` (dollars, run relu), sinon `billing.reportedCost` converti selon
    son unité (réponse du lancement), sinon `None` : pas encore réglé.
    """
    if not isinstance(run, dict):
        return None
    cost = run.get("cost")
    if (isinstance(cost, dict) and _is_number(cost.get("value"))
            and cost.get("currency") in (None, "USD")):
        return float(cost["value"])
    billing = run.get("billing")
    reported = billing.get("reportedCost") if isinstance(billing, dict) else None
    if (isinstance(reported, dict) and _is_number(reported.get("value"))
            and reported.get("currency") in (None, "USD")
            and reported.get("unit") in _COST_DIVISORS):
        return reported["value"] / _COST_DIVISORS[reported["unit"]]
    return None


def _is_number(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def _excerpt(value: Any) -> str:
    text = "" if value is None else str(value).strip()
    return text if len(text) <= 300 else text[:300] + "…"


def _non_empty_str(value: Any, name: str) -> str:
    """Rendu TEL QUEL : un endpoint ou un provider ne se réécrit pas."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"`{name}` doit être une chaîne non vide (reçu {value!r}).")
    return value


def _run_path(run_id: Any) -> str:
    """Un identifiant de run entre dans le CHEMIN : échappé, `/` compris. `quote` laisse
    le point, et `.`/`..` deviendraient des segments résolus vers une autre route."""
    if _non_empty_str(run_id, "run_id") in (".", ".."):
        raise ValueError(f"`run_id` ne peut pas être le segment de chemin {run_id!r}.")
    return f"/v1/runs/{quote(run_id, safe='')}"


def _retry_after(resp: Any) -> Optional[float]:
    """`Retry-After` en secondes ; `None` s'il manque ou ne se lit pas en nombre."""
    try:
        value = float(resp.headers.get("Retry-After"))
    except (TypeError, ValueError):
        return None
    return max(0.0, value) if math.isfinite(value) else None


def _parse(resp: Any) -> Any:
    """Le JSON du corps, ou `_NOT_JSON` — jamais un `ValueError`, qui se confondrait
    en aval avec un refus de validation des arguments."""
    if not resp.content:
        return _NOT_JSON
    try:
        return resp.json()
    except ValueError:
        return _NOT_JSON


def _is_wallet_unavailable(resp: Any) -> bool:
    """Un 503 `WalletUnavailableError` : `walletStatus` PRÉSENT, `null` compris (pas créé)."""
    data = _parse(resp) if resp.status_code == 503 else None
    return isinstance(data, dict) and "walletStatus" in data


def _unknown_outcome(why: str) -> str:
    return (f"Issue inconnue du lancement : {why}. Le run peut exister et être "
            "facturé — relire la liste des runs avant toute nouvelle tentative, "
            "relancer peut payer deux fois.")


class MonidClient:
    """Client de l'API Monid `/v1`, auth Bearer par clé d'API de workspace."""

    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None):
        """`api_key` : clé d'API Monid (sinon la variable d'env `MONID_API_KEY`) ;
        `base_url` : racine de l'API (défaut `https://api.monid.ai`)."""
        self.api_key = api_key or require_secret("MONID_API_KEY")
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.session = requests.Session()
        # Clé en EN-TÊTE uniquement : en query string elle entrerait dans l'URL,
        # donc dans le message de toute exception et dans les journaux d'accès.
        self.session.headers.update({"Authorization": f"Bearer {self.api_key}",
                                     "Accept": "application/json"})

    # --- transport ----------------------------------------------------------

    def _send(self, method: str, path: str, *, params: Optional[Dict[str, Any]],
              body: Any, timeout: Any, retry: bool) -> Any:
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        resp = None
        for attempt in range(_MAX_ATTEMPTS):
            resp = self.session.request(
                method, f"{self.base_url}{path}", params=clean or None, json=body,
                timeout=timeout, allow_redirects=False)
            if (not retry or resp.status_code not in _RETRY_STATUSES
                    or attempt == _MAX_ATTEMPTS - 1):
                break
            wait = _retry_after(resp)
            if wait is None:
                if resp.headers.get("Retry-After") is None and _is_wallet_unavailable(resp):
                    break  # portefeuille FAILED ou inexistant : re-tenter ne répare rien
                wait = float(2 ** attempt)
            elif wait > _RETRY_AFTER_MAX:
                break
            time.sleep(wait)
        return resp

    def _redirect_error(self, resp: Any, method: str, path: str) -> MonidProtocolError:
        return MonidProtocolError(
            f"Monid a répondu par une redirection (HTTP {resp.status_code}) sur "
            f"{method} {path}, qui n'est pas suivie : l'adresse de base "
            f"{self.base_url!r} ne pointe pas sur l'API.",
            request_id=resp.headers.get("x-request-id"))

    def _request(self, method: str, path: str, *,
                 params: Optional[Dict[str, Any]] = None, body: Any = None,
                 timeout: Any = _HTTP_TIMEOUT, retry: Optional[bool] = None) -> Any:
        """Transport commun. `retry` vaut par défaut « lecture seulement » (GET)."""
        if retry is None:
            retry = method == "GET"
        resp = self._send(method, path, params=params, body=body, timeout=timeout, retry=retry)
        status, request_id = resp.status_code, resp.headers.get("x-request-id")
        if 300 <= status < 400:
            raise self._redirect_error(resp, method, path)
        data = _parse(resp)
        if status >= 400:
            raise MonidHTTPError(status, resp.text if data is _NOT_JSON else data,
                                 request_id=request_id, retry_after=_retry_after(resp))
        if not resp.content:
            return {}
        if data is _NOT_JSON:
            raise MonidProtocolError(
                f"Monid a répondu {status} sans corps JSON (Content-Type "
                f"{resp.headers.get('Content-Type')!r}) sur {method} {path}.",
                request_id=request_id)
        return data

    # --- identité, portefeuille ---------------------------------------------

    def whoami(self) -> dict:
        """GET /v1/auth/whoami — `{user, workspace?}`, gratuit : la sonde de clé.
        **401** = clé absente, mal formée ou révoquée ; **403** = aucun workspace."""
        return self._request("GET", "/v1/auth/whoami")

    def wallet_balance(self) -> dict:
        """GET /v1/wallet/balance — `{balance, held}`, montants en dollars.

        `balance` est le DÉPENSABLE (vivant moins réservé), négatif possible ; `held` est
        réservé aux runs en cours. **503** avec `Retry-After` = portefeuille en création,
        re-tenté dans la borne ; SANS = en échec ou pas encore créé, levé aussitôt.
        """
        return self._request("GET", "/v1/wallet/balance")

    # --- catalogue ----------------------------------------------------------

    def discover(self, q: str, *, limit: Optional[int] = None,
                 category: Optional[str] = None,
                 min_score: Optional[float] = None) -> dict:
        """POST /v1/discover/endpoints — recherche sémantique d'endpoints.

        Rend `{items, total}` : chaque fiche porte `provider`, `endpoint`,
        `displayName`, `displayDescription`, `price`, `tags`, `categories` ; `total`
        compte les résultats au-dessus du plancher AVANT `limit`, pas de curseur.
        `category` = identifiant de catégorie ; `min_score` = plancher de
        pertinence (0 à 2). Le prix qui fait foi est celui d'`inspect`.
        """
        if not isinstance(q, str) or not 1 <= len(q.strip()) <= 1000:
            raise ValueError("`q` doit être un texte de 1 à 1000 caractères.")
        body: Dict[str, Any] = {"q": q.strip()}
        if limit is not None:
            if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
                raise ValueError(f"`limit` doit être un entier ≥ 1 (reçu {limit!r}).")
            body["limit"] = limit
        if category is not None:
            if (not isinstance(category, str) or len(category) > 60
                    or not _CATEGORY_RE.fullmatch(category)):
                raise ValueError("`category` doit être un identifiant en minuscules, chiffres "
                                 f"et tirets, 60 caractères au plus (reçu {category!r}).")
            body["category"] = category
        if min_score is not None:
            if not _is_number(min_score) or not 0 <= min_score <= 2:
                raise ValueError("`min_score` doit être un nombre entre 0 et 2 "
                                 f"(reçu {min_score!r}).")
            body["minScore"] = min_score
        return self._request("POST", "/v1/discover/endpoints", body=body)

    def inspect(self, provider: str, endpoint: str) -> dict:
        """POST /v1/inspect — la fiche complète d'un endpoint.

        Le seul endroit où se lisent le schéma d'entrée (`input` : `pathParams`,
        `queryParams`, `body` en JSON Schema, et `bodyType`) et le prix courant (`price`,
        parfois absent), avec `notes`, `metrics`, `docUrl`. `provider` et `endpoint`
        passent TELS QUE `discover` les a rendus. **404** = endpoint inconnu de ce fournisseur.
        """
        return self._request("POST", "/v1/inspect", body={
            "provider": _non_empty_str(provider, "provider"),
            "endpoint": _non_empty_str(endpoint, "endpoint")})

    # --- runs ---------------------------------------------------------------

    def run(self, provider: str, endpoint: str, *, body: Optional[dict] = None,
            query_params: Optional[dict] = None, path_params: Optional[dict] = None,
            timeout: float = RUN_READ_TIMEOUT) -> dict:
        """POST /v1/run — lance un endpoint et DÉBITE le portefeuille. Jamais re-tenté.

        L'entrée part en trois parties (`input.body`, `input.queryParams`,
        `input.pathParams`), seulement celles qui ne sont pas vides ; `input` est
        omis quand les trois le sont. `timeout` = budget de LECTURE en secondes.

        Rend le run tel que Monid le renvoie dès que le corps en est un (`runId` +
        `status`), QUEL QUE SOIT le code HTTP : 200 (`COMPLETED`, `BLOCKED`), 202
        (accepté, à suivre), 408 (`TIMED_OUT`), 502 (le fournisseur a répondu 402),
        ou le code du fournisseur recopié, 3xx compris (redirection non suivie).

        Lève `MonidHTTPError` sur l'enveloppe d'erreur de Monid (402 = portefeuille
        insuffisant ; `may_have_run` vrai sur un 5xx) ; `MonidProtocolError` sur une 3xx
        sans run, et avec `may_have_run=True` quand la requête a pu partir sans réponse
        exploitable (lecture perdue, connexion rompue ou jamais établie, corps illisible).
        Un délai de CONNEXION remonte tel quel : rien n'est parti.
        """
        payload: Dict[str, Any] = {"provider": _non_empty_str(provider, "provider"),
                                   "endpoint": _non_empty_str(endpoint, "endpoint")}
        parts: Dict[str, Any] = {}
        for key, name, value in (("body", "body", body),
                                 ("queryParams", "query_params", query_params),
                                 ("pathParams", "path_params", path_params)):
            if value is not None and not isinstance(value, dict):
                raise ValueError(f"`{name}` doit être un objet (reçu {type(value).__name__}).")
            if value:
                parts[key] = value
        if parts:
            payload["input"] = parts
        if not _is_number(timeout) or timeout <= 0:
            raise ValueError(f"`timeout` doit être un nombre de secondes > 0 (reçu {timeout!r}).")
        try:
            resp = self._send("POST", "/v1/run", params=None, body=payload,
                              timeout=(10, timeout), retry=False)
        except requests.exceptions.ConnectTimeout:
            raise
        except (requests.exceptions.ReadTimeout, requests.exceptions.ConnectionError,
                requests.exceptions.ChunkedEncodingError,
                requests.exceptions.ContentDecodingError) as exc:
            # un refus de connexion n'est pas un `ConnectTimeout` : « a pu partir »
            raise MonidProtocolError(_unknown_outcome(
                f"aucune réponse exploitable ({type(exc).__name__}), la requête a pu "
                "partir"), may_have_run=True) from exc
        status, request_id = resp.status_code, resp.headers.get("x-request-id")
        data = _parse(resp)
        if isinstance(data, dict) and all(isinstance(data.get(k), str) and data[k]
                                          for k in ("runId", "status")):
            return data  # avant la 3xx : un statut de fournisseur recopié reste un run
        if 300 <= status < 400:
            raise self._redirect_error(resp, "POST", "/v1/run")
        if data is not _NOT_JSON and status >= 400:
            err = MonidHTTPError(status, data, request_id=request_id,
                                 retry_after=_retry_after(resp))
            err.may_have_run = status >= 500
            raise err
        why = (f"HTTP {status} sans identifiant de run" if data is not _NOT_JSON else
               f"HTTP {status} sans corps JSON (Content-Type "
               f"{resp.headers.get('Content-Type')!r})")
        raise MonidProtocolError(_unknown_outcome(why), request_id=request_id,
                                 may_have_run=True)

    def get_run(self, run_id: str) -> dict:
        """GET /v1/runs/{runId} — l'état du run, `input` et `output` compris : `cost.value`
        (dollars) une fois réglé, `stoppable` tant qu'il tourne, `reason`/`controls` sur un
        `BLOCKED`. **403** = run d'un autre workspace ; **404** = run inconnu."""
        return self._request("GET", _run_path(run_id))

    def wait_for_run(self, run_id: str, *, max_wait_s: float, poll_initial: float = 1.0,
                     poll_max: float = 5.0) -> dict:
        """Relit le run jusqu'à un état final ou jusqu'à `max_wait_s`, le premier atteint.

        Rend le DERNIER état lu, terminal ou non : un run encore en cours à l'échéance
        n'est pas une erreur. Première lecture immédiate, puis un intervalle parti de
        `poll_initial`, ×1,5 à chaque tour jusqu'à `poll_max`, jamais au-delà de
        l'échéance. Sans re-tentative (la boucle en tient lieu) : une erreur HTTP remonte.
        """
        path = _run_path(run_id)
        if not _is_number(max_wait_s) or not 0 <= max_wait_s <= _MAX_WAIT_S:
            raise ValueError(f"`max_wait_s` doit être entre 0 et {_MAX_WAIT_S} "
                             f"secondes (reçu {max_wait_s!r}).")
        if (not _is_number(poll_initial) or not _is_number(poll_max)
                or not 0 < poll_initial <= poll_max):
            raise ValueError("il faut 0 < `poll_initial` ≤ `poll_max` (reçu "
                             f"{poll_initial!r}, {poll_max!r}).")
        deadline = time.monotonic() + max_wait_s
        interval = float(poll_initial)
        while True:
            remaining = deadline - time.monotonic()
            run = self._request("GET", path, retry=False,
                                timeout=(5, max(5.0, min(30.0, remaining))))
            now = time.monotonic()
            if is_terminal(run) or now >= deadline:
                return run
            time.sleep(min(interval, deadline - now))
            interval = min(interval * 1.5, float(poll_max))

    def list_runs(self, *, limit: Optional[int] = None, cursor: Optional[str] = None,
                  status: Optional[str] = None) -> dict:
        """GET /v1/runs — les runs du workspace, du plus récent au plus ancien.

        Rend `{items, cursor?}`, `cursor` absent sur la dernière page. `limit` de 1 à 100 ;
        `status` en toute casse, envoyé en majuscules. Les lignes ne portent ni `input` ni
        `output` : c'est `get_run` qui les rend.
        """
        if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int)
                                  or not 1 <= limit <= 100):
            raise ValueError(f"`limit` doit être un entier de 1 à 100 (reçu {limit!r}).")
        if cursor is not None and not isinstance(cursor, str):
            raise ValueError("`cursor` doit être la chaîne rendue par la page "
                             f"précédente (reçu {cursor!r}).")
        if status is not None:
            wanted = status.strip().upper() if isinstance(status, str) else None
            if wanted not in RUN_STATUSES:
                raise ValueError(f"`status` invalide : {status!r}. Valeurs acceptées : "
                                 + ", ".join(RUN_STATUSES))
            status = wanted
        return self._request("GET", "/v1/runs", params={
            "limit": limit, "cursor": cursor or None, "status": status})

    def stop_run(self, run_id: str) -> dict:
        """POST /v1/runs/{runId}/stop — demande l'arrêt. Jamais re-tenté.

        **202** `{runId, status: "STOPPING", message}` : l'arrêt est ASYNCHRONE, le run
        passe ensuite `STOPPED` — ou `COMPLETED` pour un endpoint facturé à l'usage, qui
        règle ce qu'il a consommé. **409** = run déjà terminé ou non arrêtable.
        """
        return self._request("POST", f"{_run_path(run_id)}/stop", retry=False)


__all__ = ["DEFAULT_BASE_URL", "MonidClient", "MonidHTTPError", "MonidProtocolError",
           "RUN_READ_TIMEOUT", "RUN_STATUSES", "SERVICE", "TERMINAL_STATUSES",
           "is_terminal", "run_cost_usd"]
