"""Origami API client — tables de leads, campagnes email + LinkedIn (origami.chat).

API v2 (`https://origami.chat/api/v2`, doc https://docs.origami.chat, spec
https://docs.origami.chat/openapi-v2.yaml), auth **Bearer** `og_live_…`. Une méthode =
un endpoint ; les corps et réponses passent tels quels (JSON), le client n'invente
aucune sémantique. Endpoints vérifiés en live les 16–17/08/2026.

Conventions v2 à connaître (elles conditionnent l'appelant) :
- **Listes** : enveloppe `{object: "list", items: [...], nextCursor: str|null, url}` —
  page de 50 par défaut ; l'appelant SUIT `nextCursor` (repassé en `cursor`).
- **Erreurs** : `{error, code, details?, handoff?}`, `code` en SNAKE_CASE majuscule
  (`UNKNOWN_FIELDS`, `TABLE_NOT_FOUND`, `MISSING_SCOPE`…) — remontées telles quelles
  dans `UpstreamHTTPError.body`.
- **Écritures** : `POST` partout (upsert de lignes, création/lancement de campagne) —
  ce client N'EST PAS lecture seule. `dryRun`/`confirm` sont des query params
  côté API ; ce sont les seuls garde-fous que le serveur offre.
- **Suppressions** : deux temps (`DELETE` sans `confirm` = aperçu d'impact, puis
  `?confirm=true`). ⚠️ Vérifié : le 2e temps peut répondre 200 SANS supprimer — un
  appelant qui veut la certitude re-GET la ressource et exige un 404.
- **Slugs** : les clés de lignes et `matchColumns` sont des SLUGS de colonnes d'entrée
  (`GET /tables/{id}/columns` → `items[].slug`, tirets), jamais des noms affichés ;
  un slug inconnu → 400 `UNKNOWN_FIELDS` ; une colonne non-input → `NON_INPUT_COLUMNS`.
- **Upload** : `POST /workspaces/{id}/documents` en JSON (octets en base64), JAMAIS
  en multipart ; un CSV en `mode: "table"` CRÉE une table.
- **Campagnes** : `POST /tables/{id}/campaigns` est AGENTIQUE (l'agent Origami
  rédige la campagne à partir d'`instructions`) → 202 `{agent: {id}, run: {id}}` ;
  on suit `GET /agents/{aid}/runs/{rid}` (il n'existe PAS de `GET /runs/{id}`).
  Il n'existe pas non plus de `GET /campaigns` global : lister par table
  (`/tables/{id}/campaigns`) ; `GET /sequences?workspaceId=` est la vue qui voit
  toutes les séquences (une par personne enrôlée) d'un workspace.
- **Projets** : la clé est parent-wide ; l'en-tête `x-origami-project: <projectId>`
  scope la requête à un projet (org enfant). Optionnel (`project_id`), omis =
  l'org parente.

Requires: requests
"""
from __future__ import annotations

import json as _json
from typing import Any, Dict, List, Optional

import requests

from ...config import require_secret
from ..common import raise_for_upstream

# (connexion, lecture) — la création de campagne et l'upload peuvent être longs.
_HTTP_TIMEOUT = (10, 120)


class OrigamiClient:
    """Client Origami v2 (https://origami.chat/api/v2), auth Bearer `og_live_…`."""

    BASE_URL = "https://origami.chat/api/v2"

    def __init__(self, api_key: Optional[str] = None,
                 project_id: Optional[str] = None):
        """
        Args:
            api_key: clé Origami (ou variable d'env `ORIGAMI_API_KEY`).
            project_id: id de projet (org enfant) → en-tête `x-origami-project`.
                Omis = la requête agit sur l'org parente de la clé.
        """
        self.api_key = api_key or require_secret("ORIGAMI_API_KEY")
        self.project_id = project_id
        self.session = requests.Session()
        # Clé en HEADER uniquement (jamais en query string : elle finirait dans l'URL,
        # donc dans le message de toute exception, les logs et Sentry).
        self.session.headers.update({
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        })
        if project_id:
            self.session.headers["x-origami-project"] = project_id

    # --- transport ----------------------------------------------------------

    def _request(self, method: str, path: str, *,
                 params: Optional[Dict[str, Any]] = None,
                 json: Any = None) -> Any:
        # Les query params à None sont retirés ; les booléens partent en `true`/`false`
        # (requests écrirait `True`, que le serveur ne lit pas comme un booléen).
        clean: Dict[str, Any] = {}
        for k, v in (params or {}).items():
            if v is None:
                continue
            clean[k] = ("true" if v else "false") if isinstance(v, bool) else v
        resp = self.session.request(
            method, f"{self.BASE_URL}{path}", params=clean or None, json=json,
            timeout=_HTTP_TIMEOUT)
        raise_for_upstream(resp, service="origami")
        return resp.json() if resp.content else {}

    # --- workspaces ---------------------------------------------------------

    def list_workspaces(self, cursor: Optional[str] = None,
                        limit: Optional[int] = None,
                        search: Optional[str] = None) -> Dict[str, Any]:
        """GET /workspaces — enveloppe liste (`items[]` de `{id, name, url,
        createdAt…}`, `nextCursor`). `search` = sous-chaîne du nom."""
        return self._request("GET", "/workspaces",
                             params={"cursor": cursor, "limit": limit, "search": search})

    def create_workspace(self, name: str) -> Dict[str, Any]:
        """POST /workspaces — crée un workspace (flux « upload d'abord ») → 201
        `{id, name, …}`. 403 `WORKSPACE_LIMIT_REACHED` si le plan est plein."""
        if not name or not str(name).strip():
            raise ValueError("create_workspace: `name` requis.")
        return self._request("POST", "/workspaces", json={"name": name})

    def upload_documents(self, workspace_id: str,
                         files: List[Dict[str, Any]]) -> Dict[str, Any]:
        """POST /workspaces/{id}/documents — LE verbe d'ingestion (JSON, jamais
        multipart). `files` = `[{filename, content (base64), mode?, tableId?}]` ;
        `mode` ∈ table (CSV → NOUVELLE table, défaut pour .csv) | append (CSV →
        table existante, `tableId` requis) | document. Préflight tout-ou-rien ;
        201 dès qu'un fichier a atterri (`results[]`, entrées `kind: "error"`
        possibles), 422 `UPLOAD_FAILED` si tous ont échoué."""
        if not files:
            raise ValueError("upload_documents: `files` vide.")
        for f in files:
            if not isinstance(f, dict) or not f.get("filename") or not f.get("content"):
                raise ValueError(
                    "upload_documents: chaque fichier = {filename, content (base64)[, mode, tableId]}.")
        return self._request("POST", f"/workspaces/{workspace_id}/documents",
                             json={"files": list(files)})

    # --- tables -------------------------------------------------------------

    def list_tables(self, workspace_id: Optional[str] = None,
                    cursor: Optional[str] = None,
                    limit: Optional[int] = None) -> Dict[str, Any]:
        """GET /tables[?workspaceId=] — enveloppe liste de tables (`{id, workspaceId,
        name, leadCount, columns[], credits, url…}`)."""
        return self._request("GET", "/tables",
                             params={"workspaceId": workspace_id, "cursor": cursor,
                                     "limit": limit})

    def get_table(self, table_id: str, include: Optional[str] = None) -> Dict[str, Any]:
        """GET /tables/{id} — nom, leadCount, colonnes, crédits consommés
        (`credits.lifetimeUsed`) ; `include="stats"` ajoute l'économie du tableau
        (creditsPerLead, qualification, funnel)."""
        return self._request("GET", f"/tables/{table_id}", params={"include": include})

    def list_columns(self, table_id: str) -> Dict[str, Any]:
        """GET /tables/{id}/columns — `items[]` de `{id, name, slug, kind, autoTrigger}`.
        Les `slug` sont les clés à utiliser dans `upsert_rows` (colonnes `kind ==
        "input"` seulement)."""
        return self._request("GET", f"/tables/{table_id}/columns")

    # --- rows ---------------------------------------------------------------

    def list_rows(self, table_id: str, cursor: Optional[str] = None,
                  cells: Optional[str] = "flat", limit: Optional[int] = None,
                  filters: Optional[List[Dict[str, Any]]] = None,
                  sort: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """GET /tables/{id}/rows?cells=flat[&cursor=] — UNE page (50 par défaut,
        `limit` ≤ 200) + `total` ; l'appelant suit `nextCursor`. `cells="flat"`
        rend `{slug: valeur}` par ligne (`None` = cellules typées polymorphes).
        `filters` = `[{column, operator, value}]` et `sort` = `{column, direction}`
        (slugs), sérialisés en JSON dans la query."""
        params: Dict[str, Any] = {"cursor": cursor, "cells": cells, "limit": limit}
        if filters:
            params["filters"] = _json.dumps(filters)
        if sort:
            params["sort"] = _json.dumps(sort)
        return self._request("GET", f"/tables/{table_id}/rows", params=params)

    def upsert_rows(self, table_id: str, rows: List[Dict[str, Any]],
                    match_columns: List[str], enrich: bool = False,
                    reenrich_updated: Optional[bool] = None,
                    batch_id: Optional[str] = None) -> Dict[str, Any]:
        """POST /tables/{id}/rows/upsert — L'unique écriture de lignes (1–100 lignes
        par appel). Une ligne dont TOUTES les valeurs de `match_columns` égalent une
        ligne existante la met à jour, sinon elle est insérée. Clés = slugs de
        colonnes d'ENTRÉE (tirets) — slug inconnu → 400 `UNKNOWN_FIELDS`, colonne
        non-input → `NON_INPUT_COLUMNS`, valeur de match vide → `MISSING_MATCH_VALUE`,
        doublon dans la requête → `DUPLICATE_MATCH_KEY`, plusieurs lignes existantes
        pour une clé → 409 `AMBIGUOUS_MATCH`.

        `enrich` : le DÉFAUT API est true (enrichir les lignes insérées, dépense des
        crédits) ; ici False par défaut — l'enrichissement se demande explicitement.
        `reenrich_updated` : ré-enrichir aussi les lignes mises à jour (re-dépense).
        Renvoie un `enrichment_run` `{id, batchId, counts: {inserted, updated,
        skipped}}` à suivre via GET /enrichment-runs/{id}."""
        if not rows:
            raise ValueError("upsert_rows: `rows` vide.")
        if len(rows) > 100:
            raise ValueError(f"upsert_rows: {len(rows)} lignes > 100 par appel — découper.")
        if not match_columns:
            raise ValueError("upsert_rows: `match_columns` requis (slugs de colonnes d'entrée).")
        body: Dict[str, Any] = {
            "rows": list(rows),
            "matchColumns": list(match_columns),
            "enrich": bool(enrich),
        }
        if reenrich_updated is not None:
            body["reenrichUpdated"] = bool(reenrich_updated)
        if batch_id:
            body["batchId"] = batch_id
        return self._request("POST", f"/tables/{table_id}/rows/upsert", json=body)

    # --- campaigns ----------------------------------------------------------

    def list_campaigns(self, table_id: str) -> Dict[str, Any]:
        """GET /tables/{id}/campaigns — les campagnes qui envoient depuis cette table
        (`items[]` de `{id, slug, name, status, peopleCount}`, `nextCursor: null`).
        Il n'existe PAS de `GET /campaigns` global."""
        return self._request("GET", f"/tables/{table_id}/campaigns")

    def get_campaign(self, campaign_id: str) -> Dict[str, Any]:
        """GET /campaigns/{id} — `{id, slug, name, status (draft|active|paused),
        workspaceId, tableId, channels: {email, linkedin}, settings:
        {blockActiveDuplicates, blockPriorContacts, autoTopUpEnabled}, brief…}`."""
        return self._request("GET", f"/campaigns/{campaign_id}")

    def campaign_stats(self, campaign_id: str) -> Dict[str, Any]:
        """GET /campaigns/{id}/stats — `{found, contacted, connectSent,
        connectAccepted, connectionRate, replied, replyRate, hasEmail, hasLinkedin}`."""
        return self._request("GET", f"/campaigns/{campaign_id}/stats")

    def campaign_people(self, campaign_id: str, cursor: Optional[str] = None,
                        limit: Optional[int] = None, status: Optional[str] = None,
                        search: Optional[str] = None) -> Dict[str, Any]:
        """GET /campaigns/{id}/people — les personnes enrôlées (une séquence par
        personne) : `{sequenceId, rowId, recipient, sendStatus, stopReason,
        fitScore, fitExplanation, profile, addedAt}` + `total`. Paginé par
        `cursor` ; `status` = CSV de statuts d'envoi ; `search` = sous-chaîne."""
        return self._request("GET", f"/campaigns/{campaign_id}/people",
                             params={"cursor": cursor, "limit": limit,
                                     "status": status, "search": search})

    def create_campaign(self, table_id: str, instructions: str,
                        settings: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """POST /tables/{id}/campaigns — création AGENTIQUE : l'agent Origami rédige
        la campagne (canaux, séquences) à partir d'`instructions` (1–10 000 car.).
        Corps `{instructions, settings?}` ; `settings` = `{blockPriorContacts,
        blockActiveDuplicates}` (vérifié live 16–17/08/2026). Répond 202
        `{agent: {id}, run: {id}, table}` — suivre `get_run(agent_id, run_id)`
        jusqu'à `status != "running"` ; la campagne apparaît ensuite dans
        `list_campaigns(table_id)`. N'envoie rien : le lancement est un autre appel.
        402 `INSUFFICIENT_CREDITS`, 409 `AGENT_BUSY`, 429 `CONCURRENT_LIMIT_EXCEEDED`."""
        if not instructions or not str(instructions).strip():
            raise ValueError("create_campaign: `instructions` requis.")
        body: Dict[str, Any] = {"instructions": instructions}
        if settings:
            body["settings"] = dict(settings)
        return self._request("POST", f"/tables/{table_id}/campaigns", json=body)

    def get_run(self, agent_id: str, run_id: str,
                include: Optional[str] = None) -> Dict[str, Any]:
        """GET /agents/{aid}/runs/{rid} — le run (`status`: running | terminal,
        `steps`, `response.tables[]`…). C'est LE chemin de suivi après une création
        de campagne — il n'existe PAS de `GET /runs/{id}`. `include="stats,transcript"`
        en option."""
        return self._request("GET", f"/agents/{agent_id}/runs/{run_id}",
                             params={"include": include})

    def launch_campaign(self, campaign_id: str, dry_run: bool = False) -> Dict[str, Any]:
        """POST /campaigns/{id}/launch[?dryRun=true] — passe la campagne `active` et
        déroule le pipeline de lancement (porte des comptes émetteurs, annulation des
        doublons, planification) : c'est l'appel qui ENVOIE. Idempotent sur une
        campagne déjà active. `dry_run=True` → `{dryRun: true, campaignId,
        wouldLaunch}` sans écriture. Le résultat réel porte `launched` et
        `launch: {scheduled, firstScheduledAt, missingRecipientCount, …,
        blocked?: {reason, message, missingChannels[]}}` — `blocked` = aucun compte
        émetteur pour ces canaux, RIEN n'est parti."""
        return self._request("POST", f"/campaigns/{campaign_id}/launch",
                             params={"dryRun": True} if dry_run else None)

    def pause_campaign(self, campaign_id: str, dry_run: bool = False) -> Dict[str, Any]:
        """POST /campaigns/{id}/pause[?dryRun=true] — met en pause (idempotent).
        Résultat réel : `pause: {stoppedSequences, haltedSteps, inFlightSending,
        alreadyPaused}`."""
        return self._request("POST", f"/campaigns/{campaign_id}/pause",
                             params={"dryRun": True} if dry_run else None)

    def resume_campaign(self, campaign_id: str, dry_run: bool = False) -> Dict[str, Any]:
        """POST /campaigns/{id}/resume[?dryRun=true] — reprend là où les séquences
        s'étaient arrêtées (idempotent). Résultat réel : `resume: {resumedSequences,
        noAccountSequences, missingChannels[]}`."""
        return self._request("POST", f"/campaigns/{campaign_id}/resume",
                             params={"dryRun": True} if dry_run else None)

    def delete_campaign(self, campaign_id: str, confirm: bool = False,
                        dry_run: bool = False) -> Dict[str, Any]:
        """DELETE /campaigns/{id}[?confirm=true][&dryRun=true] — suppression en DEUX
        temps. Sans `confirm` (ou avec `dry_run`) : aperçu d'impact `{id, name,
        confirmationRequired: true, status}`, rien n'est retiré. Avec
        `confirm=True` : soft-delete + annulation des séquences orphelines →
        `{id, name, deleted: true}`.

        ⚠️ Vérifié en live : le 2e temps peut répondre 200 sans que la campagne
        disparaisse. Un appelant qui a besoin de la certitude re-GET la campagne
        (`get_campaign`) et exige un 404 (`UpstreamHTTPError.status_code == 404`)."""
        params: Dict[str, Any] = {}
        if confirm:
            params["confirm"] = True
        if dry_run:
            params["dryRun"] = True
        return self._request("DELETE", f"/campaigns/{campaign_id}", params=params or None)

    # --- sequences ----------------------------------------------------------

    def list_sequences(self, workspace_id: str, cursor: Optional[str] = None,
                       limit: Optional[int] = None, status: Optional[str] = None,
                       channel: Optional[str] = None,
                       recipient: Optional[str] = None) -> Dict[str, Any]:
        """GET /sequences?workspaceId= — TOUTES les séquences (une par personne
        enrôlée, chaque `item` porte son `campaignId`) d'un workspace : la seule vue
        qui voit chaque campagne, quel que soit son tableau. `workspaceId` est
        requis par l'API (sinon 400 `MISSING_SCOPE`). Filtres `status` / `channel`
        / `recipient`, pagination `cursor`."""
        if not workspace_id:
            raise ValueError("list_sequences: `workspace_id` requis (400 MISSING_SCOPE sinon).")
        return self._request("GET", "/sequences",
                             params={"workspaceId": workspace_id, "cursor": cursor,
                                     "limit": limit, "status": status,
                                     "channel": channel, "recipient": recipient})

    def get_sequence(self, sequence_id: str) -> Dict[str, Any]:
        """GET /sequences/{id} — une séquence avec ses étapes inline (les internes
        fournisseur sont expurgés)."""
        return self._request("GET", f"/sequences/{sequence_id}")
