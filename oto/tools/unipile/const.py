"""Constantes et helpers de forme du connecteur Unipile.

Extrait de `client.py` (découpage par domaine) — contenu inchangé. Ces noms
restent réexportés par `oto.tools.unipile.client` : c'est le chemin d'import
historique, et il est figé.
"""

from __future__ import annotations

import re
from typing import Optional

DEFAULT_DSN = "api.unipile.com"
# (connect, read) en secondes — borne le blocage : un socket amont muet faisait
# pendre l'appel jusqu'au cutoff 300s du client MCP (unipile_me, #114).
_REQUEST_TIMEOUT = (10, 120)
# #238 : la recherche Recruiter PAR URL (talent/search) peut PENDRE indéfiniment
# quand le `searchContextId` de l'URL est expiré/mort côté LinkedIn — l'endpoint ne
# répond ni erreur ni vide → timeout MCP à 180s, opaque. Read timeout court dédié :
# on échoue AVANT le plafond MCP avec une erreur PROPRE et actionnable.
_URL_SEARCH_TIMEOUT = (10, 75)
# Scrape (recherche structurée + fiche société) : LinkedIn/Unipile peut faire
# PENDRE l'appel ~120s (surcharge / rate-limit qui queue) — vécu 2026-07-21, 166
# ReadTimeout de 121s qui gelaient l'agent 2 min chacun. Read timeout court dédié :
# échouer vite (60s) avec une erreur actionnable plutôt que geler.
_SCRAPE_TIMEOUT = (10, 60)

# Feed d'accueil LinkedIn : LinkedIn n'expose AUCUN endpoint feed côté API
# Unipile. Le seul chemin est la Magic Route Voyager, exposée en v2 comme le proxy
# générique `POST /v2/{account_id}/linkedin/` (proxyRequest) : on relaie une
# requête Voyager brute. ⚠️ Voyager n'est PAS contractuel : ce queryId GraphQL et
# le schéma JSON peuvent casser quand LinkedIn fait évoluer son API interne
# (capture devtools sur linkedin.com/feed pour le rafraîchir). Source du queryId :
# https://developer.unipile.com/docs/get-raw-data-example
FEED_QUERY_ID = "voyagerFeedDashMainFeed.7a50ef8ba5a7865c23ad5df46f735709"

# Providers dont la messagerie est rangée par INBOX. Unipile documente DEUX formes
# d'endpoint pour la même opération — « Use `GET /v2/:account_id/chats` or
# `GET /v2/:account_id/inboxes/:inbox_id/chats` **if the provider uses inboxes** »
# (guide de migration messaging v2) — et répond **501** à la mauvaise, DANS LES DEUX
# SENS. Le client ne servait que LinkedIn quand la forme inbox est arrivée (delta live
# 2026-07-06) : la bascule a été faite en dur, donc appliquée aussi à WhatsApp/Telegram/
# Instagram/Messenger/Twitter, qui n'ont pas d'inbox → 501 sur `op="list"` pour ces cinq
# canaux, alors que leur compte est bien connecté. La forme est donc DÉCLARÉE par
# provider (ci-dessous) — pas devinée par canal appelant, pas figée à un seul modèle.
_INBOX_PROVIDERS = {"LINKEDIN"}
# Provider supposé quand l'appelant n'en déclare pas : le client est historiquement
# LinkedIn-first (`UNIPILE_LINKEDIN_ACCOUNT_ID`, découverte du 1er compte linkedin), et
# un appelant qui ne dit rien attend le comportement d'avant.
_DEFAULT_PROVIDER = "LINKEDIN"

# Préfixe de path par produit LinkedIn (search & co.).
_API_PREFIX = {
    "classic": "/linkedin/search",
    "sales_navigator": "/linkedin/sales-navigator/search",
    "recruiter": "/linkedin/recruiter/search",
}


def cursor_with_limit(cursor: str, limit: int) -> str:
    """Réécrit `limit` DANS un cursor Unipile (base64 de `{limit, startIndex}`).

    L'API Unipile fige le `limit` du 1er appel dans le cursor et IGNORE ensuite
    le param `limit` (feedback #179 : une pagination entamée à limit=3 restait
    bloquée à 3/page — des centaines d'appels pour un réseau entier). Le limit
    de l'appel courant doit primer. Forme de cursor inattendue (non-base64,
    non-JSON, pas de clé limit) → rendu tel quel, l'API tranche."""
    import base64
    import json
    try:
        data = json.loads(base64.b64decode(cursor + "=" * (-len(cursor) % 4)))
        if isinstance(data, dict) and "limit" in data:
            data["limit"] = int(limit)
            return base64.b64encode(json.dumps(data).encode()).decode()
    except Exception:  # noqa: BLE001 — cursor opaque : jamais bloquant
        pass
    return cursor


def _sections_param(sections: str) -> Optional[list[str]]:
    """Map la valeur `sections` vers le param v2 `with_sections`.

    Entrée : `"*"` (tout) ou une liste séparée par virgules de noms nus
    (`experience`, `education`…). v2 : `with_sections=linkedin_<nom>` (et
    `linkedin_*` = tout). `"*"`/vide → None (défaut serveur = tout)."""
    s = (sections or "").strip()
    if not s or s in ("*", "linkedin_*"):
        return None
    out: list[str] = []
    for part in s.split(","):
        p = part.strip()
        if not p:
            continue
        out.append(p if p.startswith("linkedin_") else f"linkedin_{p}")
    return out or None


def _slug_from_company_url(url: str) -> Optional[str]:
    """Extrait le slug d'une URL LinkedIn société (`…/company/<slug>[/…]`)."""
    if not url:
        return None
    m = re.search(r"/company/([^/?#]+)", url)
    return m.group(1) if m else None
