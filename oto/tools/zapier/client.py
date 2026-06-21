"""Zapier AI Actions API client — actions exposées + exécution.

Zapier est une plateforme d'automatisation (« Zaps »). Plutôt qu'une API de
gestion des Zaps, Zapier expose pour les agents l'**AI Actions API**
(`actions.zapier.com`) : un catalogue d'**actions** que l'utilisateur a
explicitement exposées (ex. « créer une ligne Google Sheets », « envoyer un
Slack »), exécutables en langage naturel + paramètres.

Auth = **API key** (en-tête `x-api-key`). La clé se crée sur
https://actions.zapier.com/credentials/ (chaque clé porte le jeu d'actions
exposées par l'utilisateur).

Clé passée au constructeur (ou `ZAPIER_API_KEY` en fallback).

Docs : https://actions.zapier.com/docs/

Requires: requests
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import requests

from ...config import require_secret


class ZapierClient:
    """Client Zapier AI Actions — liste + exécution d'actions exposées."""

    BASE_URL = "https://actions.zapier.com/api/v1"

    def __init__(self, api_key: Optional[str] = None):
        """Initialise le client.

        Args:
            api_key: Zapier AI Actions API key (ou env `ZAPIER_API_KEY`).
        """
        self.api_key = api_key or require_secret("ZAPIER_API_KEY")
        self.session = requests.Session()
        self.session.headers.update({
            "x-api-key": self.api_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
        })

    def _request(self, method: str, path: str, **kwargs) -> Any:
        url = f"{self.BASE_URL}{path}"
        resp = self.session.request(method, url, timeout=60, **kwargs)
        if resp.status_code >= 400:
            try:
                body = resp.json()
            except Exception:
                body = resp.text
            raise Exception(f"Zapier HTTP {resp.status_code}: {body}")
        return resp.json() if resp.content else {}

    def list_actions(self) -> Dict[str, Any]:
        """Liste les actions exposées par cette clé (id, description, params).

        Chaque action porte un `id` (à passer à `execute_action`) et la liste de
        ses champs paramétrables."""
        return self._request("GET", "/exposed/")

    def execute_action(
        self,
        action_id: str,
        instructions: str,
        params: Optional[Dict[str, Any]] = None,
        preview_only: bool = False,
    ) -> Dict[str, Any]:
        """Exécute une action exposée.

        Args:
            action_id: id de l'action (cf. `list_actions`).
            instructions: consigne en langage naturel — Zapier remplit les
                champs laissés en mode « AI guess » à partir de ce texte.
            params: surcharges explicites des champs de l'action (priment sur
                la déduction depuis `instructions`).
            preview_only: True = ne pas exécuter, renvoyer ce qui serait fait.
        """
        body: Dict[str, Any] = {"instructions": instructions}
        if params:
            body.update(params)
        if preview_only:
            body["preview_only"] = True
        return self._request("POST", f"/exposed/{action_id}/execute/", json=body)

    def execution_log(self, execution_log_id: str) -> Dict[str, Any]:
        """Récupère le détail d'une exécution (`execution_log_id` renvoyé par
        `execute_action`)."""
        return self._request("GET", f"/execution-log/{execution_log_id}/")
