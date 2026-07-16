"""Unipile connector — hosted LinkedIn (and other IM) via the Unipile API v2.

Unipile maintient la session LinkedIn côté serveur (vrai Chrome + proxy
résidentiel), ce qui contourne les deux contraintes du browser local : empreinte
TLS et isolation de session (le cookie ne vit pas sur notre IP datacenter, donc
n'expose ni ne déconnecte la session de l'utilisateur). Cf. oto-mcp#5.
"""

from .client import UnipileClient, UnipileError, parse_feed


def make_unipile_client(api_key=None, dsn=None, account_id=None):
    """Factory du client Unipile (construction seam consommée par oto-mcp)."""
    return UnipileClient(api_key=api_key, dsn=dsn, account_id=account_id)


__all__ = ["UnipileClient", "UnipileError", "parse_feed", "make_unipile_client"]
