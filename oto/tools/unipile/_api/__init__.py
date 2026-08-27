"""Familles d'appels Unipile, composées dans `UnipileClient`.

Un module par domaine d'API. Détail du contrat : `../client.py`.
"""

from .accounts import _AccountsMixin
from .content import _ContentMixin
from .messaging import _MessagingMixin
from .network import _NetworkMixin
from .premium import _PremiumMixin
from .profiles import _ProfilesMixin
from .search import _SearchMixin

__all__ = [
    "_AccountsMixin",
    "_ContentMixin",
    "_MessagingMixin",
    "_NetworkMixin",
    "_PremiumMixin",
    "_ProfilesMixin",
    "_SearchMixin",
]
