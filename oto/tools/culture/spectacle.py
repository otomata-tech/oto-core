"""Ré-export — `SpectacleClient` (licences entrepreneurs spectacles vivants, LES)
a migré dans `france-opendata` (lib data publique FR partagée). Conservé pour la
rétrocompat des imports `oto.tools.culture` (oto-mcp `tools/culture.py` importe
`SpectacleClient`). Ne rien ajouter ici : éditer `france_opendata.culture_spectacle`.
"""
from france_opendata.culture_spectacle import (  # noqa: F401
    SpectacleClient,
    PORTAL,
    DATASET,
    STATUS_VALUES,
    CATEGORIES,
)
