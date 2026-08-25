"""Airtable Web API client — https://airtable.com/developers/web/api/introduction

Airtable = des **bases** (`appXXXX`), chacune faite de **tables** (`tblXXXX`) dont les
colonnes sont des **champs typés** (`fldXXXX`) et les lignes des **records** (`recXXXX`).
Ce client couvre toute la section « Base data » de la Web API (records, commentaires,
pièces jointes, sync CSV) plus le schéma (tables/champs) et la liste des bases, sans
lesquels un appelant ne peut ni choisir une base ni écrire un champ.

Auth : **Personal Access Token** (`Authorization: Bearer pat…`), créé sur
https://airtable.com/create/tokens. Un PAT porte des **scopes** ET une **liste de bases
explicitement accordées** — les deux sont nécessaires (cf. `whoami` / `list_bases`).

Deux hôtes : `https://api.airtable.com/v0` pour tout, SAUF l'upload de pièce jointe qui
vit sur `https://content.airtable.com/v0`.

**Une méthode = un endpoint.** Aucune boucle ici : ni pagination automatique, ni
découpage en lots, ni délai de courtoisie. C'est délibéré — les trois demandent un
budget de temps et une reddition de comptes partielle (« 30 records écrits sur 50, puis
429 ») qui appartiennent à l'appelant, pas au client. Les méthodes de lot **refusent**
plus de `MAX_RECORDS_PER_REQUEST` records au lieu de découper en douce.

Limites de l'API à connaître AVANT d'appeler :

- **10 records maximum par requête** en create / update / delete
  (`MAX_RECORDS_PER_REQUEST`). Non documenté lisiblement côté Airtable ; c'est la valeur
  qu'appliquent les clients officiels et pyairtable.
- **5 requêtes/seconde par base**, 50/s par token. Au-delà : **429**, et Airtable exige
  **30 secondes** d'attente avant que les requêtes suivantes repassent. Un appelant sous
  contrainte de temps a donc intérêt à s'arrêter et rendre compte plutôt qu'à attendre.
- `list_records` rend **100 records par page** au maximum, et un `offset` opaque tant
  qu'il en reste.
- `sync_csv` : 10 000 lignes, 500 colonnes, **2 Mo par requête**, et une limite propre de
  **20 requêtes / 5 minutes / base**.
- `upload_attachment` : **5 Mo**, contenu en **base64**. Au-delà, passer par une URL
  publique dans le champ pièce jointe (`{"url": …}` via `update_record`).
- `cell_format="string"` **exige** `time_zone` ET `user_locale` — sinon 422.

⚠️ **`typecast=True` n'est pas une simple conversion de type : c'est une mutation de
schéma déclenchée par une écriture de donnée.** Sur un single/multi-select il **crée
l'option manquante** ; sur un champ *linked record* il **crée un enregistrement dans la
table liée**. Et il ne demande que le scope `data.records:write`, jamais
`schema.bases:write`. Le défaut de ce client est donc `typecast` **non transmis**
(= `false` côté Airtable) : une valeur inattendue échoue franchement au lieu d'élargir en
silence le schéma d'une base réelle.

⚠️ **Noms de champs vs identifiants.** Les tables et les champs s'adressent par nom ou
par id. Le **nom change** dès qu'un humain renomme une colonne, et casse alors
l'automatisation en silence — `tbl…`/`fld…` sont le chemin stable. `return_fields_by_field_id`
demande à l'API de rendre les valeurs keyées par id de champ plutôt que par nom.

Requires: requests
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional
from urllib.parse import quote

import requests

from ...config import require_secret
from ..common import raise_for_upstream

_HTTP_TIMEOUT = (10, 60)  # (connexion, lecture) — jamais d'attente illimitée


class AirtableClient:
    """Client Airtable Web API v0, auth PAT Bearer."""

    BASE_URL = "https://api.airtable.com/v0"
    # L'upload de pièce jointe est le SEUL endpoint servi par un autre hôte.
    CONTENT_URL = "https://content.airtable.com/v0"

    #: Plafond DUR de l'API sur create/update/delete multiples.
    MAX_RECORDS_PER_REQUEST = 10

    def __init__(self, api_key: Optional[str] = None):
        """
        Args:
            api_key: Personal Access Token (ou variable d'env `AIRTABLE_API_KEY`).
        """
        self.api_key = api_key or require_secret("AIRTABLE_API_KEY")

    # ------------------------------------------------------------------
    # Plomberie

    def _headers(self, content_type: str = "application/json") -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": content_type,
            "Accept": "application/json",
        }

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: Optional[Dict[str, Any]] = None,
        base_url: Optional[str] = None,
        data: Optional[str] = None,
        content_type: str = "application/json",
    ) -> Any:
        """Un appel HTTP. `data` = corps BRUT (sync CSV), exclusif avec `json`."""
        resp = requests.request(
            method,
            f"{base_url or self.BASE_URL}{path}",
            headers=self._headers(content_type),
            json=json,
            data=data.encode("utf-8") if isinstance(data, str) else data,
            params=params,
            timeout=_HTTP_TIMEOUT,
        )
        raise_for_upstream(resp, service="airtable")
        return resp.json() if resp.content else None

    @staticmethod
    def _clean(params: Dict[str, Any]) -> Dict[str, Any]:
        """Retire les clés à `None` — un paramètre non fourni ne doit pas partir."""
        return {k: v for k, v in params.items() if v is not None}

    @staticmethod
    def _qbool(value: Optional[bool]) -> Optional[str]:
        """Booléen destiné à la QUERY STRING — `requests` sérialise `True` en `"True"`,
        qu'Airtable ne reconnaît pas (les clients de référence coercent en `true`/`1`).
        Ne concerne que les params GET ; dans un corps JSON le booléen part tel quel."""
        return None if value is None else ("true" if value else "false")

    @staticmethod
    def _seg(value: str) -> str:
        """Un segment d'URL. Un NOM de table ou de champ peut contenir espaces et `/`."""
        return quote(str(value), safe="")

    def _check_batch(self, records: List[Any], verb: str) -> None:
        if len(records) > self.MAX_RECORDS_PER_REQUEST:
            raise ValueError(
                f"airtable {verb}: {len(records)} records pour un maximum de "
                f"{self.MAX_RECORDS_PER_REQUEST} par requête. Découper côté appelant "
                f"(et espacer les requêtes : 5/s par base)."
            )

    @staticmethod
    def _sort_params(sort: Optional[List[Dict[str, str]]]) -> Dict[str, str]:
        """`[{"field": "Name", "direction": "desc"}]` → `sort[0][field]`, `sort[0][direction]`."""
        out: Dict[str, str] = {}
        for i, rule in enumerate(sort or []):
            out[f"sort[{i}][field]"] = rule["field"]
            if rule.get("direction"):
                out[f"sort[{i}][direction]"] = rule["direction"]
        return out

    @staticmethod
    def _cell_format_params(
        cell_format: Optional[str], time_zone: Optional[str], user_locale: Optional[str]
    ) -> Dict[str, Any]:
        """`cellFormat="string"` exige `timeZone` ET `userLocale` — refusé ici, pas en 422."""
        if cell_format == "string" and not (time_zone and user_locale):
            raise ValueError(
                "airtable: cell_format='string' exige time_zone (ex. 'Europe/Paris') "
                "ET user_locale (ex. 'fr'). Sinon utiliser cell_format='json'."
            )
        return {"cellFormat": cell_format, "timeZone": time_zone, "userLocale": user_locale}

    # ==================================================================
    # Records — https://airtable.com/developers/web/api/list-records
    # ==================================================================

    def list_records(
        self,
        base_id: str,
        table: str,
        *,
        fields: Optional[List[str]] = None,
        filter_by_formula: Optional[str] = None,
        max_records: Optional[int] = None,
        page_size: Optional[int] = None,
        sort: Optional[List[Dict[str, str]]] = None,
        view: Optional[str] = None,
        cell_format: Optional[str] = None,
        time_zone: Optional[str] = None,
        user_locale: Optional[str] = None,
        return_fields_by_field_id: Optional[bool] = None,
        record_metadata: Optional[List[str]] = None,
        offset: Optional[str] = None,
    ) -> Dict[str, Any]:
        """`GET /{baseId}/{table}` — UNE page de records (100 max).

        Rend `{"records": [{id, createdTime, fields}], "offset": …}`. L'`offset` n'est
        présent que s'il reste des pages ; le repasser tel quel pour la suivante.

        `filter_by_formula` est une formule Airtable évaluée par ligne
        (ex. `{Status}='Done'`). `record_metadata=["commentCount"]` ajoute le nombre de
        commentaires. `fields` restreint les colonnes rendues — le premier levier contre
        une réponse énorme.
        """
        params = self._clean({
            "filterByFormula": filter_by_formula,
            "maxRecords": max_records,
            "pageSize": page_size,
            "view": view,
            "offset": offset,
            "returnFieldsByFieldId": self._qbool(return_fields_by_field_id),
            "fields[]": fields,
            "recordMetadata[]": record_metadata,
            **self._cell_format_params(cell_format, time_zone, user_locale),
        })
        params.update(self._sort_params(sort))
        return self._request("GET", f"/{base_id}/{self._seg(table)}", params=params)

    def list_records_post(self, base_id: str, table: str, body: Dict[str, Any]) -> Dict[str, Any]:
        """`POST /{baseId}/{table}/listRecords` — même chose, critères dans le CORPS.

        Échappatoire d'Airtable quand la query string devient trop longue (une grosse
        `filterByFormula`). `body` reprend les mêmes clés qu'en GET, en camelCase.
        """
        return self._request("POST", f"/{base_id}/{self._seg(table)}/listRecords", json=body)

    def get_record(
        self,
        base_id: str,
        table: str,
        record_id: str,
        *,
        cell_format: Optional[str] = None,
        time_zone: Optional[str] = None,
        user_locale: Optional[str] = None,
        return_fields_by_field_id: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """`GET /{baseId}/{table}/{recordId}` — un record avec tous ses champs."""
        params = self._clean({
            "returnFieldsByFieldId": self._qbool(return_fields_by_field_id),
            **self._cell_format_params(cell_format, time_zone, user_locale),
        })
        return self._request(
            "GET", f"/{base_id}/{self._seg(table)}/{record_id}", params=params
        )

    def create_records(
        self,
        base_id: str,
        table: str,
        records: List[Dict[str, Any]],
        *,
        typecast: Optional[bool] = None,
        return_fields_by_field_id: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """`POST /{baseId}/{table}` — crée 1 à 10 records.

        `records` = `[{"fields": {"Name": "Ada"}}, …]`. Rend `{"records": [...]}` avec les
        `recXXXX` attribués. Au-delà de 10 : `ValueError` (voir `_check_batch`).
        """
        self._check_batch(records, "create_records")
        body = self._clean({
            "records": records,
            "typecast": typecast,
            "returnFieldsByFieldId": return_fields_by_field_id,
        })
        return self._request("POST", f"/{base_id}/{self._seg(table)}", json=body)

    def update_record(
        self,
        base_id: str,
        table: str,
        record_id: str,
        fields: Dict[str, Any],
        *,
        replace: bool = False,
        typecast: Optional[bool] = None,
        return_fields_by_field_id: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """`PATCH` (ou `PUT` si `replace`) `/{baseId}/{table}/{recordId}`.

        ⚠️ `replace=True` → **PUT destructif** : tout champ absent du corps est VIDÉ.
        `PATCH` (défaut) ne touche que les champs transmis.
        """
        body = self._clean({
            "fields": fields,
            "typecast": typecast,
            "returnFieldsByFieldId": return_fields_by_field_id,
        })
        return self._request(
            "PUT" if replace else "PATCH",
            f"/{base_id}/{self._seg(table)}/{record_id}",
            json=body,
        )

    def update_records(
        self,
        base_id: str,
        table: str,
        records: List[Dict[str, Any]],
        *,
        replace: bool = False,
        typecast: Optional[bool] = None,
        perform_upsert: Optional[Dict[str, Any]] = None,
        return_fields_by_field_id: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """`PATCH` (ou `PUT` si `replace`) `/{baseId}/{table}` — 1 à 10 records.

        Deux régimes :
        - **update** : chaque item porte son `id` (`{"id": "rec…", "fields": {…}}`).
        - **upsert** : `perform_upsert={"fieldsToMergeOn": ["Email"]}` (1 à 3 champs).
          Les items SANS `id` sont alors rapprochés des lignes existantes sur ces
          champs — trouvées ⟹ mises à jour, sinon créées. La réponse distingue
          `createdRecords` et `updatedRecords`.

        ⚠️ `replace=True` (PUT) vide les champs non transmis, y compris en upsert.
        """
        self._check_batch(records, "update_records")
        body = self._clean({
            "records": records,
            "typecast": typecast,
            "performUpsert": perform_upsert,
            "returnFieldsByFieldId": return_fields_by_field_id,
        })
        return self._request(
            "PUT" if replace else "PATCH", f"/{base_id}/{self._seg(table)}", json=body
        )

    def delete_record(self, base_id: str, table: str, record_id: str) -> Dict[str, Any]:
        """`DELETE /{baseId}/{table}/{recordId}` — suppression DÉFINITIVE d'une ligne."""
        return self._request("DELETE", f"/{base_id}/{self._seg(table)}/{record_id}")

    def delete_records(self, base_id: str, table: str, record_ids: List[str]) -> Dict[str, Any]:
        """`DELETE /{baseId}/{table}?records[]=…` — 1 à 10 lignes, DÉFINITIVEMENT."""
        self._check_batch(record_ids, "delete_records")
        return self._request(
            "DELETE", f"/{base_id}/{self._seg(table)}", params={"records[]": record_ids}
        )

    # ==================================================================
    # Commentaires — https://airtable.com/developers/web/api/list-comments
    # ==================================================================

    def list_comments(
        self,
        base_id: str,
        table: str,
        record_id: str,
        *,
        page_size: Optional[int] = None,
        offset: Optional[str] = None,
    ) -> Dict[str, Any]:
        """`GET /{baseId}/{table}/{recordId}/comments` — du plus récent au plus ancien.

        100 par page maximum. Chaque commentaire porte `author`, `text`,
        `parentCommentId` (réponse dans un fil), `reactions` et `mentioned`.
        """
        return self._request(
            "GET",
            f"/{base_id}/{self._seg(table)}/{record_id}/comments",
            params=self._clean({"pageSize": page_size, "offset": offset}),
        )

    def create_comment(
        self,
        base_id: str,
        table: str,
        record_id: str,
        text: str,
        *,
        parent_comment_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """`POST /{baseId}/{table}/{recordId}/comments`.

        Mentionner quelqu'un s'écrit `@[usrXXXXXXX]` dans `text`. `parent_comment_id`
        répond dans un fil existant.
        """
        body = self._clean({"text": text, "parentCommentId": parent_comment_id})
        return self._request(
            "POST", f"/{base_id}/{self._seg(table)}/{record_id}/comments", json=body
        )

    def update_comment(
        self, base_id: str, table: str, record_id: str, comment_id: str, text: str
    ) -> Dict[str, Any]:
        """`PATCH /{baseId}/{table}/{recordId}/comments/{commentId}` — seul `text` change.

        Un PAT ne peut éditer que les commentaires de SON propre utilisateur.
        """
        return self._request(
            "PATCH",
            f"/{base_id}/{self._seg(table)}/{record_id}/comments/{comment_id}",
            json={"text": text},
        )

    def delete_comment(
        self, base_id: str, table: str, record_id: str, comment_id: str
    ) -> Dict[str, Any]:
        """`DELETE /{baseId}/{table}/{recordId}/comments/{commentId}`.

        Supprimer le commentaire de tête d'un fil supprime le fil entier.
        """
        return self._request(
            "DELETE", f"/{base_id}/{self._seg(table)}/{record_id}/comments/{comment_id}"
        )

    # ==================================================================
    # Pièces jointes — autre HÔTE (content.airtable.com)
    # ==================================================================

    def upload_attachment(
        self,
        base_id: str,
        record_id: str,
        field: str,
        *,
        filename: str,
        content_type: str,
        file_b64: str,
    ) -> Dict[str, Any]:
        """`POST content.airtable.com/v0/{baseId}/{recordId}/{field}/uploadAttachment`.

        `file_b64` = le contenu du fichier encodé en **base64** (5 Mo max). AJOUTE une
        pièce jointe au champ, sans écraser les précédentes. Rend le record mis à jour.

        Au-delà de 5 Mo : héberger le fichier et poser `[{"url": …}]` dans le champ via
        `update_record` — Airtable va le chercher lui-même.
        """
        return self._request(
            "POST",
            f"/{base_id}/{record_id}/{self._seg(field)}/uploadAttachment",
            json={"contentType": content_type, "file": file_b64, "filename": filename},
            base_url=self.CONTENT_URL,
        )

    # ==================================================================
    # Sync CSV — corps BRUT text/csv
    # ==================================================================

    def sync_csv(self, base_id: str, table: str, sync_id: str, csv_data: str) -> Any:
        """`POST /{baseId}/{table}/sync/{apiEndpointSyncId}` — corps `text/csv` brut.

        Alimente une table **« Sync API »** : la table doit avoir été créée dans Airtable
        via ce mode de synchronisation, ce qui produit le `apiEndpointSyncId` (réglages
        de la table synchronisée). Ce n'est PAS un import dans une table ordinaire.

        Chaque envoi REMPLACE le contenu synchronisé (c'est une source, pas un append).
        Limites : 10 000 lignes, 500 colonnes, 2 Mo, **20 requêtes / 5 min / base**.
        """
        return self._request(
            "POST",
            f"/{base_id}/{self._seg(table)}/sync/{sync_id}",
            data=csv_data,
            content_type="text/csv",
        )

    # ==================================================================
    # Schéma de base — tables et champs
    # ==================================================================

    def get_base_schema(
        self, base_id: str, *, include: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """`GET /meta/bases/{baseId}/tables` — TOUTES les tables d'une base.

        Chaque table rend `id`, `name`, `description`, `primaryFieldId`, ses `fields`
        (`id`, `name`, `type`, `options`) et ses `views`. C'est la seule lecture du
        schéma : il n'y a pas d'endpoint « get one table ».

        `include=["visibleFieldIds"]` ajoute, pour les vues grille, les champs visibles.
        Scope `schema.bases:read`.
        """
        return self._request(
            "GET",
            f"/meta/bases/{base_id}/tables",
            params=self._clean({"include[]": include}),
        )

    def create_table(
        self,
        base_id: str,
        name: str,
        fields: List[Dict[str, Any]],
        *,
        description: Optional[str] = None,
    ) -> Dict[str, Any]:
        """`POST /meta/bases/{baseId}/tables` — nouvelle table dans une base existante.

        `fields` = `[{"name": …, "type": …, "options": {…}}]`. ⚠️ **Le PREMIER champ
        devient le champ primaire** et doit être d'un type admis comme tel (texte,
        nombre, date, formule… pas une pièce jointe ni une case à cocher).
        Scope `schema.bases:write`.
        """
        body = self._clean({"name": name, "fields": fields, "description": description})
        return self._request("POST", f"/meta/bases/{base_id}/tables", json=body)

    def update_table(
        self,
        base_id: str,
        table_id: str,
        *,
        name: Optional[str] = None,
        description: Optional[str] = None,
    ) -> Dict[str, Any]:
        """`PATCH /meta/bases/{baseId}/tables/{tableId}` — renomme / redécrit une table.

        Seuls `name` et `description` sont modifiables ; la structure passe par les
        champs. Scope `schema.bases:write`.
        """
        body = self._clean({"name": name, "description": description})
        return self._request("PATCH", f"/meta/bases/{base_id}/tables/{table_id}", json=body)

    def create_field(
        self,
        base_id: str,
        table_id: str,
        name: str,
        type: str,
        *,
        description: Optional[str] = None,
        options: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """`POST /meta/bases/{baseId}/tables/{tableId}/fields` — nouvelle colonne.

        `type` = un type Airtable (`singleLineText`, `number`, `singleSelect`,
        `multipleRecordLinks`, `checkbox`…). `options` est **exigé par la plupart des
        types** et sa forme dépend du type (un `singleSelect` veut `{"choices": [{"name":
        …}]}`, un `number` veut `{"precision": 0}`, un `multipleRecordLinks` veut
        `{"linkedTableId": …}`). Scope `schema.bases:write`.
        """
        body = self._clean({
            "name": name, "type": type, "description": description, "options": options
        })
        return self._request(
            "POST", f"/meta/bases/{base_id}/tables/{table_id}/fields", json=body
        )

    def update_field(
        self,
        base_id: str,
        table_id: str,
        field_id: str,
        *,
        name: Optional[str] = None,
        description: Optional[str] = None,
    ) -> Dict[str, Any]:
        """`PATCH /meta/bases/{baseId}/tables/{tableId}/fields/{fieldId}`.

        Seuls `name` et `description` sont modifiables : l'API **ne change ni le type ni
        les `options`** d'un champ existant. Vérifié en live le 2026-08-25 — un PATCH
        portant `options` rend `422 INVALID_REQUEST_UNKNOWN, "Changing a field's type or
        number precision is not currently supported."`, et il n'existe **aucun**
        `DELETE …/fields/{id}` (404). Une option de select ajoutée par erreur (via
        `typecast`) ne se retire donc QUE dans l'interface Airtable.
        Scope `schema.bases:write`.
        """
        body = self._clean({"name": name, "description": description})
        return self._request(
            "PATCH", f"/meta/bases/{base_id}/tables/{table_id}/fields/{field_id}", json=body
        )

    # ==================================================================
    # Bases et identité du token
    # ==================================================================

    def list_bases(self, *, offset: Optional[str] = None) -> Dict[str, Any]:
        """`GET /meta/bases` — les bases ACCORDÉES au token (1000 par page).

        Rend `{"bases": [{id, name, permissionLevel}], "offset": …}`.
        ⚠️ Un PAT parfaitement valide auquel aucune base n'a été accordée rend ici une
        **liste vide, avec un 200** — pas une erreur. C'est le mode d'échec le plus
        fréquent d'Airtable. Scope `schema.bases:read`.
        """
        return self._request("GET", "/meta/bases", params=self._clean({"offset": offset}))

    def create_base(
        self, name: str, workspace_id: str, tables: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """`POST /meta/bases` — nouvelle base dans un workspace (`wspXXXX`).

        `tables` a la même forme que dans `create_table` (au moins une table, dont le
        premier champ sera le champ primaire). Il n'existe **pas** d'endpoint de
        suppression de base dans la Web API. Scope `schema.bases:write`.
        """
        return self._request(
            "POST", "/meta/bases",
            json={"name": name, "workspaceId": workspace_id, "tables": tables},
        )

    def whoami(self) -> Dict[str, Any]:
        """`GET /meta/whoami` — l'utilisateur du token (`id`, `email` si scope, `scopes`).

        N'exige aucun scope : c'est la sonde d'authentification pure. Elle ne dit RIEN
        des bases accessibles — croiser avec `list_bases`.
        """
        return self._request("GET", "/meta/whoami")
