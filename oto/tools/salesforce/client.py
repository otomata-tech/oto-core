"""Salesforce REST API Client — https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/"""

import base64
import hashlib
import time
from typing import Any, Callable, Optional

import requests

from ...config import require_secret, get_secret
from ..common import raise_for_upstream

_HTTP_TIMEOUT = (10, 60)  # (connexion, lecture) — jamais d'attente illimitée


class SalesforceAuthError(ValueError):
    """Refus OAuth Salesforce (invalid_client / invalid_grant…).

    Contrat `UpstreamHTTPError` : porte un `status_code` 401 synthétique pour que
    les consommateurs classent ce refus de credential comme erreur gérée, pas un
    bug. Sous-classe `ValueError` : les `except ValueError` existants tiennent.
    """

    status_code = 401


# Cache de jeton d'accès PROCESS-WIDE, keyé par hash du credential — même motif que
# `oto.tools.zoho.auth._TOKEN_CACHE`, et pour la même raison : côté serveur, une
# instance de client est créée à CHAQUE appel MCP.
# {clé: (access_token, instance_url, expires_at)}
_TOKEN_CACHE: dict[str, tuple[str, str, float]] = {}


def _cred_key(login_url: str, client_id: str, refresh_token: str) -> str:
    """Isole les credentials entre eux SANS jamais garder un secret en clair comme
    clé. Le refresh_token en fait partie : après rotation, la clé change, donc une
    entrée liée à l'ancien jeton n'est jamais resservie."""
    return hashlib.sha256(
        f"{login_url}|{client_id}|{refresh_token}".encode()).hexdigest()


class SalesforceClient:
    API_VERSION = "v60.0"

    # Champs par défaut par sObject standard (évite d'exiger un describe() avant
    # toute lecture, comme Zoho DEFAULT_FIELDS).
    DEFAULT_FIELDS = {
        "Contact": "Id,FirstName,LastName,Email,Phone,Title,AccountId",
        "Account": "Id,Name,Website,Industry,Phone,BillingCity,BillingCountry",
        "Lead": "Id,FirstName,LastName,Company,Email,Phone,Status",
        "Opportunity": "Id,Name,StageName,Amount,CloseDate,AccountId",
    }

    def __init__(
        self,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        refresh_token: Optional[str] = None,
        login_url: Optional[str] = None,
        on_refresh: Optional[Callable[[dict], None]] = None,
    ):
        """Initialise le client.

        Les credentials peuvent être passés explicitement (usage serveur
        multi-utilisateur : chaque appel construit un client avec les creds
        résolus du user) ou résolus via `require_secret` (usage CLI). Le token
        d'accès ET l'`instance_url` (renvoyé par le refresh — pas de table de
        région fixe comme Zoho) sont mis en cache **en mémoire** sur l'instance —
        jamais sur un fichier partagé (qui fuiterait entre utilisateurs côté
        serveur).

        ⚠️ Ce cache d'instance ne suffit pas côté serveur : une instance est créée à
        CHAQUE appel MCP, donc il ne sert jamais. Le vrai cache est **process-wide**,
        keyé par un hash du credential (`_TOKEN_CACHE`), même motif que
        `oto.tools.zoho.auth` — sans lui on rafraîchit à chaque appel d'outil, ce qui
        sous rotation revient à faire tourner le jeton à chaque appel.

        `on_refresh(token_data)` est appelé après chaque rafraîchissement réussi,
        avec la réponse complète du serveur de jetons. C'est le seul moyen pour
        l'appelant de voir ce que Salesforce renvoie — et notamment un
        **`refresh_token` renouvelé** : sous rotation (RTR, obligatoire sur les
        External Client Apps), chaque échange invalide le jeton précédent et en
        renvoie un neuf. Le jeter — ce que faisait cette classe — révoque la
        connexion dès le premier usage.
        """
        self.client_id = client_id or require_secret("SALESFORCE_CLIENT_ID")
        self.client_secret = client_secret or require_secret("SALESFORCE_CLIENT_SECRET")
        self.refresh_token = refresh_token or require_secret("SALESFORCE_REFRESH_TOKEN")
        self.login_url = (login_url or get_secret(
            "SALESFORCE_LOGIN_URL", "https://login.salesforce.com")).rstrip("/")
        self._access_token: Optional[str] = None
        self._instance_url: Optional[str] = None
        self._token_expires_at: float = 0.0
        self._on_refresh = on_refresh

    # --- Auth ---

    def _get_access_token(self) -> tuple[str, str]:
        """Get a valid (access_token, instance_url), refreshing if needed (in-memory
        cache). The refresh_token grant doesn't report an `expires_in` — cache for a
        fixed window and re-refresh on 401, like the Zoho client."""
        if self._access_token and self._token_expires_at > time.time():
            return self._access_token, self._instance_url  # type: ignore[return-value]

        # Cache PROCESS-WIDE (motif `oto.tools.zoho.auth`) : le serveur crée un client
        # par appel MCP, donc le cache d'instance ci-dessus ne sert jamais. Sans lui on
        # rafraîchit à chaque appel d'outil — et sous rotation (RTR), rafraîchir c'est
        # faire tourner le jeton. La clé est un HASH du credential, jamais un secret en
        # clair, et elle inclut le refresh_token : un jeton renouvelé produit une clé
        # neuve, donc aucune entrée périmée n'est servie après rotation.
        k = _cred_key(self.login_url, self.client_id, self.refresh_token)
        cached = _TOKEN_CACHE.get(k)
        if cached and cached[2] > time.time() + 60:
            self._access_token, self._instance_url, self._token_expires_at = cached
            return self._access_token, self._instance_url

        resp = requests.post(
            f"{self.login_url}/services/oauth2/token",
            data={
                "grant_type": "refresh_token",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": self.refresh_token,
            },
            timeout=_HTTP_TIMEOUT,
        )
        # Salesforce suit l'OAuth2 standard (RFC 6749 §5.2) : un refus auth
        # (invalid_grant/invalid_client) renvoie HTTP 400 avec l'erreur dans le
        # corps — CONTRAIREMENT à Zoho qui renvoie 200 (cf. ZohoClient). Le corps
        # est donc parsé AVANT `raise_for_status()` : sinon le 400 lève un
        # `requests.HTTPError` brut (pas de `.status_code` direct, corps absent du
        # message) et `SalesforceAuthError` — le contrat 401 attendu par le tri
        # Sentry amont (`before_send`) — n'est jamais atteint.
        try:
            token_data = resp.json()
        except ValueError:
            resp.raise_for_status()
            raise  # 2xx mais corps illisible — remonte l'erreur de parsing d'origine

        if "error" in token_data:
            raise SalesforceAuthError(
                f"Salesforce OAuth error: {token_data['error']} — "
                f"{token_data.get('error_description', '')}")

        resp.raise_for_status()

        self._access_token = token_data["access_token"]
        self._instance_url = token_data["instance_url"]
        self._token_expires_at = time.time() + 3600
        _TOKEN_CACHE[k] = (self._access_token, self._instance_url,
                           self._token_expires_at)

        # ROTATION (RTR) : sous ce régime — obligatoire sur les External Client
        # Apps — Salesforce invalide le jeton qu'on vient d'utiliser et en renvoie
        # un neuf ici. On l'adopte pour la suite de la vie de cette instance, et on
        # laisse l'appelant le persister via `on_refresh` : sans ça, le credential
        # stocké est révoqué dès le premier appel, et toute réutilisation ultérieure
        # fait révoquer par Salesforce le jeton courant ET les access tokens
        # associés — la connexion tombe et exige une reconnexion humaine.
        rotated = token_data.get("refresh_token")
        if rotated:
            self.refresh_token = rotated
        if self._on_refresh is not None:
            # Best-effort : une panne de persistance ne doit pas faire échouer un
            # appel dont le jeton d'accès, lui, est valide.
            try:
                self._on_refresh(token_data)
            except Exception:  # noqa: BLE001 — la persistance est un effet de bord
                pass
        return self._access_token, self._instance_url

    def _invalidate_token(self):
        """Forget the cached token to force a refresh on next request.

        Purge AUSSI l'entrée process-wide : ne vider que le cache d'instance
        laisserait le prochain appel MCP resservir le jeton qu'on vient de juger
        mort (un 401 relancerait alors la même requête à l'identique)."""
        _TOKEN_CACHE.pop(
            _cred_key(self.login_url, self.client_id, self.refresh_token), None)
        self._access_token = None
        self._token_expires_at = 0.0

    # --- HTTP ---

    def _url_for(self, instance_url: str, path: str) -> str:
        """`path` relative to `/services/data/{API_VERSION}/` (e.g. "sobjects/Account/",
        "query/") — or an absolute path starting with "/" (e.g. a `nextRecordsUrl`),
        used as-is against `instance_url`."""
        if path.startswith("/"):
            return f"{instance_url}{path}"
        return f"{instance_url}/services/data/{self.API_VERSION}/{path}"

    def _request(self, method: str, path: str, **kwargs) -> Any:
        token, instance_url = self._get_access_token()
        url = self._url_for(instance_url, path)
        headers = {"Authorization": f"Bearer {token}"}

        for attempt in range(2):
            resp = requests.request(method, url, headers=headers, **kwargs)

            # Token expired — refresh once and retry
            if resp.status_code == 401 and attempt == 0:
                self._invalidate_token()
                token, instance_url = self._get_access_token()
                url = self._url_for(instance_url, path)
                headers["Authorization"] = f"Bearer {token}"
                continue

            # No 429 branch: unlike Zoho, Salesforce's REST API doesn't use HTTP 429
            # for rate limiting — exceeding the org's daily API request allotment
            # returns HTTP 403 with error code REQUEST_LIMIT_EXCEEDED (confirmed
            # against Salesforce's own error-codes reference). It's a rolling 24h
            # quota, not a per-second throttle, so an immediate retry wouldn't help
            # anyway — `raise_for_upstream` surfaces the 403 + REQUEST_LIMIT_EXCEEDED
            # body as-is, which is already actionable.
            raise_for_upstream(resp, service="salesforce")

            # create = 201 + body ; update/delete = 204 no body
            return resp.json() if resp.content else {}

        raise Exception("Request failed after retries")

    # --- sObjects (generic CRUD) ---

    def describe(self, sobject: str) -> dict:
        """Field metadata for an sObject type (Account, Contact, custom…)."""
        return self._request("GET", f"sobjects/{sobject}/describe/")

    def get_record(self, sobject: str, record_id: str, fields: Optional[str] = None) -> dict:
        """Get a single record by id."""
        params = {"fields": fields} if fields else None
        return self._request("GET", f"sobjects/{sobject}/{record_id}", params=params)

    def list_records(
        self,
        sobject: str,
        fields: Optional[str] = None,
        where: Optional[str] = None,
        limit: int = 200,
    ) -> dict:
        """List records of an sObject type (built as a SOQL SELECT)."""
        if not fields:
            fields = self.DEFAULT_FIELDS.get(sobject)
            if not fields:
                raise ValueError(
                    f"No default fields for sObject '{sobject}'. Pass fields "
                    f"explicitly. Known sObjects: {', '.join(self.DEFAULT_FIELDS)}"
                )
        soql = f"SELECT {fields} FROM {sobject}"
        if where:
            soql += f" WHERE {where}"
        soql += f" LIMIT {limit}"
        return self.query(soql)

    def create_record(self, sobject: str, data: dict) -> dict:
        """Create a record. Returns {id, success, errors}."""
        return self._request("POST", f"sobjects/{sobject}/", json=data)

    def update_record(self, sobject: str, record_id: str, data: dict) -> dict:
        """Update a record's fields. Salesforce returns 204 — synthesized
        {id, success} (the caller never sees the raw empty body)."""
        self._request("PATCH", f"sobjects/{sobject}/{record_id}", json=data)
        return {"id": record_id, "success": True}

    def delete_record(self, sobject: str, record_id: str) -> dict:
        """Delete a record. Salesforce returns 204 — synthesized {id, success}."""
        self._request("DELETE", f"sobjects/{sobject}/{record_id}")
        return {"id": record_id, "success": True}

    def upsert_record(self, sobject: str, external_id_field: str, external_id: str, data: dict) -> dict:
        """Upsert on an external id field — idempotent create-or-update."""
        return self._request(
            "PATCH", f"sobjects/{sobject}/{external_id_field}/{external_id}", json=data)

    # --- sObject Collections (bulk create/update, ONE sObject type per call) ---
    #
    # Three Salesforce mechanisms cover "many records" and they aren't
    # interchangeable: Bulk API 2.0 is a fully ASYNC job/upload/poll lifecycle
    # meant for 2000+ records (wrong shape for a synchronous tool call); Composite
    # Batch runs up to 25 arbitrary, independent sub-requests (built for
    # heterogeneous multi-step sequences, not bulk of one type). Collections is
    # the one that fits here: synchronous, ONE HTTP call, up to 200 records of
    # the SAME sObject type (confirmed against Salesforce's REST API guide).

    MAX_COLLECTION_RECORDS = 200

    def _collection_records(self, sobject: str, items: list[dict]) -> list[dict]:
        if len(items) > self.MAX_COLLECTION_RECORDS:
            raise ValueError(
                f"{len(items)} records — sObject Collections caps at "
                f"{self.MAX_COLLECTION_RECORDS} per call.")
        return [{"attributes": {"type": sobject}, **item} for item in items]

    def create_records(self, sobject: str, items: list[dict],
                        all_or_none: bool = False) -> list[dict]:
        """Bulk-create up to 200 records of the SAME sObject type in ONE call.

        Returns a list of `{id, success, errors}`, in the SAME ORDER as
        `items`. ⚠️ A 200 HTTP response can still carry PER-RECORD failures
        when `all_or_none` is false (the Salesforce default, kept as our
        default too) — unlike `create_record`, a raised exception here only
        means the whole request failed, not that every record did. Always
        inspect each entry.
        """
        return self._request("POST", "composite/sobjects", json={
            "allOrNone": all_or_none,
            "records": self._collection_records(sobject, items),
        })

    def update_records(self, sobject: str, items: list[dict],
                        all_or_none: bool = False) -> list[dict]:
        """Bulk-update up to 200 records of the SAME sObject type in ONE call
        — each item MUST carry its own `Id` (the record to update) alongside
        the fields to change. Same per-record `{id, success, errors}` contract
        as `create_records`."""
        return self._request("PATCH", "composite/sobjects", json={
            "allOrNone": all_or_none,
            "records": self._collection_records(sobject, items),
        })

    # --- SOQL / SOSL ---

    def query(self, soql: str) -> dict:
        """Run a SOQL query. Returns {totalSize, done, records: [...], nextRecordsUrl?}."""
        return self._request("GET", "query/", params={"q": soql})

    def query_more(self, next_records_url: str) -> dict:
        """Follow pagination — `next_records_url` = a previous query's `nextRecordsUrl`
        (an absolute path, e.g. "/services/data/v60.0/query/01g...-2000"). Routed
        through `_request` for the same refresh-and-retry-once behavior as any
        other call."""
        return self._request("GET", next_records_url)

    def search(self, sosl: str) -> dict:
        """Run a SOSL search, e.g. "FIND {Acme} IN ALL FIELDS RETURNING Account(Id, Name)"."""
        return self._request("GET", "search/", params={"q": sosl})

    # --- Notes (Enhanced Notes: ContentNote + ContentDocumentLink) ---
    #
    # Salesforce replaced the classic `Note` sObject (a simple ParentId-attached
    # record, like Zoho's Notes) with "Enhanced Notes" as the Lightning Experience
    # default years ago — Enhanced Notes are backed by `ContentNote` (a specialized
    # ContentDocument whose `Content` field is a directly SOQL-queryable base64 blob)
    # linked to a parent record via a separate `ContentDocumentLink` row
    # (`ContentDocumentId` = the ContentNote's own `Id`, `LinkedEntityId` = the
    # parent). Orgs still on classic Notes (admin never enabled Enhanced Notes)
    # aren't covered here — only the modern default is.

    @staticmethod
    def _soql_escape(value: str) -> str:
        return value.replace("\\", "\\\\").replace("'", "\\'")

    def list_notes(self, parent_id: str) -> list[dict]:
        """List Enhanced Notes attached to a record, `Content` decoded back to text.

        Two SOQL queries (ContentDocumentLink → ContentNote) because
        ContentDocumentLink doesn't support a semi-join directly into ContentNote.
        """
        escaped_id = self._soql_escape(parent_id)
        links = self.query(
            "SELECT ContentDocumentId FROM ContentDocumentLink "
            f"WHERE LinkedEntityId = '{escaped_id}'"
        ).get("records", [])
        doc_ids = [r["ContentDocumentId"] for r in links]
        if not doc_ids:
            return []
        ids_csv = ",".join(f"'{i}'" for i in doc_ids)
        notes = self.query(
            f"SELECT Id, Title, Content, CreatedDate FROM ContentNote WHERE Id IN ({ids_csv})"
        ).get("records", [])
        for note in notes:
            if note.get("Content"):
                try:
                    note["Content"] = base64.b64decode(note["Content"]).decode(
                        "utf-8", errors="replace")
                except (ValueError, TypeError):
                    pass  # malformed/non-base64 — leave as returned
        return notes

    def create_note(self, parent_id: str, title: str, body: str) -> dict:
        """Create an Enhanced Note and link it to a record.

        `body` is plain text/HTML, base64-encoded for ContentNote's `Content`
        field per the API contract.
        """
        content_b64 = base64.b64encode(body.encode("utf-8")).decode("ascii")
        note = self.create_record("ContentNote", {"Title": title, "Content": content_b64})
        self.create_record("ContentDocumentLink", {
            "ContentDocumentId": note["id"],
            "LinkedEntityId": parent_id,
            "ShareType": "V",
            "Visibility": "AllUsers",
        })
        return note
