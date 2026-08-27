"""Familles d'opérations Slides, composées dans `SlidesClient`.

Un module par famille. Détail du contrat : `../slides_client.py`.
"""

from .copy import _CopyMixin
from .images import _ImagesMixin
from .layouts import _LayoutsMixin
from .presentations import _PresentationsMixin
from .text_edit import _TextEditMixin
from .text_style import _TextStyleMixin

__all__ = [
    "_CopyMixin",
    "_ImagesMixin",
    "_LayoutsMixin",
    "_PresentationsMixin",
    "_TextEditMixin",
    "_TextStyleMixin",
]
