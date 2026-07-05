"""Unipile connector — hosted LinkedIn (and other IM) via the Unipile API.

Unipile maintient la session LinkedIn côté serveur (vrai Chrome + proxy
résidentiel), ce qui contourne les deux contraintes du browser local : empreinte
TLS et isolation de session (le cookie ne vit pas sur notre IP datacenter, donc
n'expose ni ne déconnecte la session de l'utilisateur). Cf. oto-mcp#5.

Deux versions d'API cohabitent (`make_unipile_client(api_version=…)`) :
- **v1** (`UnipileClient`) — le défaut, en prod.
- **v2** (`UnipileClientV2`) — surface publique identique, API v2 Unipile (beta :
  nouveau compte + migration de données requis). Opt-in par config côté oto-mcp.
"""

from .client import UnipileClient
from .client_v2 import UnipileClientV2


def make_unipile_client(
    api_key=None, dsn=None, account_id=None, api_version="v1"
):
    """Factory : renvoie le client Unipile de la version demandée (mêmes
    méthodes publiques). `api_version` ∈ {"v1", "v2"} (défaut "v1")."""
    if str(api_version).lower().lstrip("v") == "2":
        return UnipileClientV2(api_key=api_key, dsn=dsn, account_id=account_id)
    return UnipileClient(api_key=api_key, dsn=dsn, account_id=account_id)


__all__ = ["UnipileClient", "UnipileClientV2", "make_unipile_client"]
