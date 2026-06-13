"""Ré-export — `OpendatasoftClient` a migré dans `france-opendata` (lib data
publique FR partagée). Conservé pour la rétrocompat des imports
`oto.tools.culture`. Ne rien ajouter ici : éditer `france_opendata.opendatasoft`.
"""
from france_opendata.opendatasoft import OpendatasoftClient  # noqa: F401
