"""Accords d'entreprise (index ACCO) — client HTTP vers `/api/fr/accords/*`.

L'index vit dans le service FOD, sur un réseau privé : un poste ne peut pas
l'interroger directement. On passe donc par oto-mcp, qui le republie — même
raison d'être que `SireneStock`, et même contrat d'authentification.

Auth : token long-lived `OTO_API_KEY` (dashboard → « cli & api tokens »).
Override d'URL : `OTO_API_URL`.
"""

from .client import AccordsClient, AccordsError

__all__ = ["AccordsClient", "AccordsError"]
