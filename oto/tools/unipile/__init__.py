"""Unipile connector — hosted LinkedIn (and other IM) via the Unipile API.

Unipile maintient la session LinkedIn côté serveur (vrai Chrome + proxy
résidentiel), ce qui contourne les deux contraintes du browser local : empreinte
TLS et isolation de session (le cookie ne vit pas sur notre IP datacenter, donc
n'expose ni ne déconnecte la session de l'utilisateur). Cf. oto-mcp#5.
"""

from .client import UnipileClient

__all__ = ["UnipileClient"]
