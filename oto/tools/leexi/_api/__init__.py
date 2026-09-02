"""Familles d'appels Leexi, composées dans `LeexiClient`.

Un module par domaine d'API. Détail du contrat : `../client.py`.
"""

from .calls import _CallsMixin
from .meetings import _MeetingsMixin
from .notes import _NotesMixin
from .teams import _TeamsMixin
from .users import _UsersMixin

__all__ = [
    "_CallsMixin",
    "_MeetingsMixin",
    "_NotesMixin",
    "_TeamsMixin",
    "_UsersMixin",
]
