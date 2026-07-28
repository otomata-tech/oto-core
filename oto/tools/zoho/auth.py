"""Auth OAuth2 Zoho — source unique du refresh de token pour TOUS les produits Zoho
(CRM, Desk, Analytics).

Deux incidents ont motivé la factorisation (les trois clients dupliquaient ce bloc) :

- **Fuite de secrets (#284, CRITIQUE)** : le refresh passait les credentials en
  `params=`, donc dans la QUERY STRING. `raise_for_status()` lève alors une
  `HTTPError` dont le message contient l'URL complète — `client_id`,
  `client_secret` ET `refresh_token` en clair se retrouvaient dans le transcript
  de l'agent, les logs et tout export. Ici les credentials passent en **`data=`**
  (corps form-encodé, la forme prescrite par RFC 6749 §2.3.1) : ils ne sont plus
  dans l'URL, donc plus dans aucun message d'erreur, ni dans les access logs de
  Zoho. En défense en profondeur, on n'appelle PAS `raise_for_status()` : on
  construit nous-mêmes un message rédigé.

- **Rate-limit du refresh (#233 puis #285)** : côté serveur une NOUVELLE instance
  de client est créée à CHAQUE appel MCP → un cache porté par l'instance ne sert
  jamais → un refresh par appel → Zoho rate-limite `/oauth/v2/token` et TOUT
  casse pendant plusieurs minutes. Le cache est donc **process-wide**, keyé par
  credential. Le correctif n'avait été appliqué qu'à Analytics ; le passer ici le
  donne aux trois produits d'un coup.

Clé de cache = **hash** de `accounts_url|client_id|refresh_token` : isole les
credentials entre eux (jamais de token partagé entre deux orgs/users) sans jamais
utiliser un secret en clair comme clé de dictionnaire.
"""
from __future__ import annotations

import hashlib
import time
from typing import Optional

import requests

# {cred_key: (access_token, expires_at_epoch)}
_TOKEN_CACHE: dict[str, tuple[str, float]] = {}

_SAFE_ERR = ("Échec du refresh OAuth Zoho (HTTP {status}) sur {host} : {detail}. "
             "Vérifie client_id / client_secret / refresh_token et la région "
             "(data_center) du connecteur.")


class ZohoAuthError(ValueError):
    """Refus OAuth Zoho (invalid_client / invalid_code / invalid_grant…).

    Zoho répond HTTP 200 avec l'erreur dans le corps — on porte donc un
    `status_code` 401 synthétique (contrat `UpstreamHTTPError`) pour que les
    consommateurs classent ce refus de credential comme erreur gérée, pas un
    bug. Sous-classe `ValueError` : les `except ValueError` existants tiennent.
    """

    status_code = 401


def cred_key(accounts_url: str, client_id: str, refresh_token: str) -> str:
    """Identifiant opaque et stable d'un credential (jamais un secret en clair)."""
    return hashlib.sha256(
        f"{accounts_url}|{client_id}|{refresh_token}".encode()).hexdigest()


def _host(url: str) -> str:
    """`accounts.zoho.eu` depuis une URL — sûr à afficher (aucun secret)."""
    return (url or "").split("//")[-1].split("/")[0] or "accounts.zoho.com"


def get_access_token(accounts_url: str, client_id: str, client_secret: str,
                     refresh_token: str, *, key: Optional[str] = None) -> str:
    """Token d'accès valide pour ce credential, rafraîchi seulement si nécessaire.

    Aucun secret ne transite par l'URL ni par les messages d'erreur.
    """
    k = key or cred_key(accounts_url, client_id, refresh_token)
    cached = _TOKEN_CACHE.get(k)
    if cached and cached[1] > time.time() + 60:
        return cached[0]

    resp = requests.post(
        f"{accounts_url}/oauth/v2/token",
        data={  # ⚠️ `data=`, JAMAIS `params=` : les secrets ne doivent pas
                # atterrir dans l'URL (cf. #284, docstring du module).
            "grant_type": "refresh_token",
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
        },
        timeout=30,
    )

    # Pas de `raise_for_status()` : son message embarque l'URL de la requête.
    if resp.status_code >= 400:
        detail = "refus du serveur d'autorisation"
        try:
            payload = resp.json()
            detail = payload.get("error") or payload.get("message") or detail
        except ValueError:
            pass
        raise ZohoAuthError(_SAFE_ERR.format(
            status=resp.status_code, host=_host(accounts_url), detail=detail))

    try:
        token_data = resp.json()
    except ValueError:
        raise ZohoAuthError(_SAFE_ERR.format(
            status=resp.status_code, host=_host(accounts_url),
            detail="réponse illisible (JSON attendu)"))

    # Zoho renvoie HTTP 200 + {"error": "invalid_client"} sur une région ou un
    # client faux, et invalid_code / invalid_grant sur un refresh token mort.
    if "error" in token_data:
        raise ZohoAuthError(f"Zoho OAuth error: {token_data['error']}")
    if "access_token" not in token_data:
        raise ZohoAuthError(_SAFE_ERR.format(
            status=resp.status_code, host=_host(accounts_url),
            detail="aucun access_token dans la réponse"))

    token = token_data["access_token"]
    _TOKEN_CACHE[k] = (token, time.time() + int(token_data.get("expires_in", 3600)))
    return token


def invalidate(key: str) -> None:
    """Oublie le token caché de ce credential (appelé sur 401 amont)."""
    _TOKEN_CACHE.pop(key, None)
