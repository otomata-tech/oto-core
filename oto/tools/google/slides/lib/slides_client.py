#!/usr/bin/env python3
"""
Google Slides API client for generating presentations

Structure du package (découpage 2026-08-27, surface publique INCHANGÉE) :
`slides_client.py` porte la classe `SlidesClient` — résolution des
credentials et construction des services — et compose les familles
d'opérations de `_api/` (présentations & Drive, layouts, styles de texte,
édition de texte, images, copie de slides). Les helpers de mise en forme
vivent dans `markup.py` et restent **réexportés ici** : `slides_client` est
le chemin d'import du connecteur.
"""
import os
import json
from google.oauth2 import service_account
from googleapiclient.discovery import build

from ._api import (
    _CopyMixin,
    _ImagesMixin,
    _LayoutsMixin,
    _PresentationsMixin,
    _TextEditMixin,
    _TextStyleMixin,
)
from .markup import _hex_to_rgb, parse_bold_markdown

# Surface figée : `parse_bold_markdown` / `_hex_to_rgb` restent importables
# depuis ce module, comme avant le découpage.
__all__ = ["SlidesClient", "parse_bold_markdown", "_hex_to_rgb"]


class SlidesClient(
    _PresentationsMixin,
    _LayoutsMixin,
    _TextStyleMixin,
    _TextEditMixin,
    _ImagesMixin,
    _CopyMixin,
):
    """Client for Google Slides API operations"""

    SCOPES = [
        'https://www.googleapis.com/auth/presentations',
        'https://www.googleapis.com/auth/drive'
    ]

    def __init__(self, credentials_json=None, account=None):
        """
        Initialize Slides client.

        Resolution order (premier qui répond gagne) :
        1. `credentials_json` (path or JSON string) — service account legacy
        2. `GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON` env var — service account legacy
        3. OAuth user credentials via `oto.tools.google.credentials.get_user_credentials`
           (avec ou sans nom d'`account`). Préféré pour manipuler des fichiers
           du Drive personnel d'un utilisateur.

        Args:
            credentials_json: Path to service account JSON or JSON string (legacy)
            account: OAuth account name (None = auto-detect single account)
        """
        # 1) Service account explicit
        if credentials_json is None:
            credentials_json = os.getenv('GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON')

        if credentials_json:
            if os.path.isfile(credentials_json):
                credentials = service_account.Credentials.from_service_account_file(
                    credentials_json, scopes=self.SCOPES)
            else:
                credentials_info = json.loads(credentials_json)
                credentials = service_account.Credentials.from_service_account_info(
                    credentials_info, scopes=self.SCOPES)
        else:
            # 2) OAuth user credentials (preferred for personal Drive ops)
            from oto.tools.google.credentials import get_user_credentials
            credentials = get_user_credentials(self.SCOPES, account=account)

        self.slides_service = build('slides', 'v1', credentials=credentials)
        self.drive_service = build('drive', 'v3', credentials=credentials)
