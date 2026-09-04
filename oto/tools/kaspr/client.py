"""
Kaspr API Client for LinkedIn profile enrichment.

Requires: requests
"""

import re
from typing import Optional, Dict, Any, List

import requests

from ...config import require_secret

_HTTP_TIMEOUT = (10, 60)  # (connexion, lecture) — jamais d'attente illimitée

# Kaspr veut le SLUG NU : une URL complète (ou un slash/query) fait un 500
# (vérifié live : `alexislaporte` → 200, `https://.../in/alexislaporte/` → 500).
_LINKEDIN_IN = re.compile(r"/in/([^/?#]+)", re.IGNORECASE)

# Les noms que Kaspr accepte dans `dataToGet` — l'enum de SON OpenAPI (`info.version`
# 2.0, `items.enum` du corps de `POST /profile/linkedin`), pas notre souvenir.
# Un nom hors enum ne rend pas un 400 lisible : le parser amont plante et l'appelant
# reçoit un **500** (`TypeError: Cannot read properties of undefined (reading
# 'push')`) — c'est-à-dire une panne, là où il a une faute de frappe. Reproduit le
# 2026-09-01 sur un profil sentinelle, sans consommer de crédit :
#   ["emails", "phones", "company"] → 500 ;  ["workEmail", "phone"] → 402 ;  [] → 200.
#
# Ces trois noms-là n'étaient pas un hasard, et c'est la leçon du lot : `emails`,
# `phones` et `company` sont des noms de champs de la RÉPONSE Kaspr (`personalEmails`,
# `phones`, `company`) — repris comme s'ils étaient des valeurs d'ENTRÉE, puis figés
# dans la docstring du tool MCP `kaspr_enrich_linkedin` dès sa création (2026-05-22).
# Un agent qui lit le schéma applique ce qu'il y lit. D'où le refus LOCAL ci-dessous,
# qui NOMME les valeurs acceptées : un premier essai corrigeable au lieu d'une panne
# qu'on croit amont — et qu'on réessaie donc en boucle.
DATA_TO_GET = ("workEmail", "directEmail", "phone")

# ⚠️ `personalEmail` est TOLÉRÉ, pas documenté : il ne figure pas dans l'enum de Kaspr,
# où le mail personnel s'appelle `directEmail`. Il traîne dans nos docstrings depuis la
# création du client et n'a JAMAIS été mesuré — il a donc exactement la forme du défaut
# qu'on corrige ici (`personalEmails` est, lui, un champ de la réponse). On l'accepte
# quand même : le refuser casserait un appelant qui l'utilise peut-être avec succès, et
# ce lot n'a pas à trancher par supposition ce qu'une sonde d'une seconde trancherait.
# À mesurer sur l'id sentinelle (500 ⇒ le retirer d'ici et le traiter comme `emails`).
DATA_TO_GET_TOLERES = ("personalEmail",)

_ACCEPTES = DATA_TO_GET + DATA_TO_GET_TOLERES

# Ce que Kaspr reçoit quand l'appelant de CE client ne demande rien. ⚠️ Ce n'est pas
# le défaut de l'API : omis, Kaspr sélectionne « all allowed fields ». Mais le client
# ne l'omet jamais — il substitue la liste ci-dessous. « Defaults to all » était donc
# faux ici, quoi qu'en dise la doc du fournisseur.
DATA_TO_GET_DEFAUT = ["workEmail", "phone"]


def linkedin_slug(raw: str) -> str:
    """Normalise un identifiant LinkedIn (slug nu OU URL profil) → slug nu."""
    raw = (raw or "").strip()
    m = _LINKEDIN_IN.search(raw)
    if m:
        return m.group(1)
    return raw.rstrip("/").split("?")[0].split("#")[0]


class KasprClient:
    """
    Kaspr API client for:
    - LinkedIn profile enrichment
    - Email and phone number retrieval
    """

    BASE_URL = "https://api.developers.kaspr.io"
    # (connect, read) — Kaspr répond <1s en nominal ; sans read-timeout un blip
    # amont laisse l'appel suspendu POUR TOUJOURS (thread perdu, jamais loggé —
    # vécu 2026-07-22, signal #252 : appel invisible du calllog, client MCP parti
    # à 60s, serveur pendu). Un timeout transforme le blip en erreur actionnable.
    TIMEOUT = (10, 50)

    def __init__(self, api_key: str = None):
        """
        Initialize Kaspr client.

        Args:
            api_key: Kaspr API key (or set KASPR_API_KEY env var)
        """
        self.api_key = api_key or require_secret("KASPR_API_KEY")

    def _request(self, method: str, endpoint: str, **kwargs) -> Dict[str, Any]:
        """Make API request."""
        url = f"{self.BASE_URL}/{endpoint}"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "accept-version": "v2.0",
        }

        kwargs.setdefault("timeout", self.TIMEOUT)
        response = requests.request(method, url, headers=headers, **kwargs)
        response.raise_for_status()
        return response.json()

    def verify_key(self) -> Dict[str, Any]:
        """
        Validate the API key.

        Kaspr v2.0 n'expose pas d'endpoint `/user` ou `/me` — on vérifie
        l'auth via un POST sentinel sur `/profile/linkedin` avec un id
        manifestement introuvable. L'API authentifie avant de chercher le
        profil donc on obtient 401 si la clé est mauvaise, 200 + body
        vide sinon (vérifié live 22/05).

        Returns: `{"valid": True}` si clé OK, sinon lève la HTTPError.
        """
        self._request(
            "POST", "profile/linkedin",
            json={
                "id": "__oto_verify_key__",
                "name": "__verify__",
                "dataToGet": [],
            },
        )
        return {"valid": True}

    def enrich_linkedin(
        self,
        linkedin_id: str,
        name: str = None,
        is_phone_required: bool = False,
        data_to_get: List[str] = None,
    ) -> Dict[str, Any]:
        """
        Enrich a LinkedIn profile.

        Args:
            linkedin_id: LinkedIn slug ("john-doe-12345") or full profile URL
                ("https://www.linkedin.com/in/john-doe-12345/") — the bare slug
                is extracted automatically (a full URL makes Kaspr 500).
            name: Full name (helps matching)
            is_phone_required: Require phone number
            data_to_get: field names to retrieve — Kaspr's own enum, i.e.
                `DATA_TO_GET` ("workEmail", "directEmail", "phone"), plus the
                tolerated `DATA_TO_GET_TOLERES`. Anything else is refused HERE,
                because Kaspr answers 500 on a name it does not know.
                Omitted → `DATA_TO_GET_DEFAUT` (NOT every field).

        Returns:
            Enriched profile with emails and phones

        Raises:
            ValueError: `data_to_get` carries a name Kaspr does not know.
        """
        if data_to_get is not None:
            inconnus = [str(d) for d in data_to_get if d not in _ACCEPTES]
            if inconnus:
                raise ValueError(
                    "Kaspr n'accepte dans `dataToGet` que "
                    + ", ".join(DATA_TO_GET)
                    + " — reçu : " + ", ".join(inconnus)
                    + ". (Un nom inconnu ne fait pas un refus chez Kaspr : "
                      "il fait une erreur serveur 500.)")
        slug = linkedin_slug(linkedin_id)
        data = {"id": slug, "name": name or slug}
        if is_phone_required:
            data["isPhoneRequired"] = True
        data["dataToGet"] = data_to_get or list(DATA_TO_GET_DEFAUT)

        try:
            return self._request("POST", "profile/linkedin", json=data)
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 402 and "phone" in data["dataToGet"]:
                data["dataToGet"] = [d for d in data["dataToGet"] if d != "phone"]
                return self._request("POST", "profile/linkedin", json=data)
            raise
