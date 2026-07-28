"""Client HTTP de l'index ACCO (accords d'entreprise), exposé par oto-mcp."""

import os
from typing import Any, Dict, List, Optional

import requests

from oto.config import require_secret

_DEFAULT_BASE_URL = "https://mcp.oto.cx"


class AccordsError(RuntimeError):
    def __init__(self, status: int, detail: Any):
        self.status = status
        self.detail = detail
        super().__init__(f"accords API {status}: {detail}")


class AccordsClient:
    """Recherche dans l'index ACCO via `/api/fr/accords/*`.

    Example:
        acc = AccordsClient()
        res = acc.search(idcc="1486", themes=["111", "112"], limit=20)
        one = acc.get("ACCOTEXT000054284583")
    """

    def __init__(self, base_url: Optional[str] = None, token: Optional[str] = None):
        self.base_url = (
            base_url or os.environ.get("OTO_API_URL") or _DEFAULT_BASE_URL
        ).rstrip("/")
        self.token = token or require_secret("OTO_API_KEY")
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        })

    def _raise(self, r: requests.Response) -> Any:
        if r.status_code >= 400:
            try:
                detail = r.json()
            except Exception:
                detail = r.text
            raise AccordsError(r.status_code, detail)
        return r.json()

    def search(self, query: Optional[str] = None, themes: Optional[List[str]] = None,
               nature: Optional[str] = None, siren: Optional[str] = None,
               siret: Optional[str] = None, idcc: Optional[str] = None,
               departement: Optional[str] = None, date_from: Optional[str] = None,
               date_to: Optional[str] = None, latest_per_siret: bool = False,
               sort_by: str = "date", sort_dir: str = "desc",
               limit: int = 50, offset: int = 0) -> Dict[str, Any]:
        """Page de résultats + `total_count` (renvoyé même avec limit=1 : de quoi
        dimensionner une campagne sans rapatrier les lignes).

        `idcc` : un code de convention. L'index le stocke sans zéro de tête, le
        serveur accepte les deux formes — « 0573 » comme « 573 ».
        """
        payload = {
            "query": query, "themes": themes, "nature": nature, "siren": siren,
            "siret": siret, "idcc": idcc, "departement": departement,
            "date_from": date_from, "date_to": date_to,
            "latest_per_siret": latest_per_siret,
            "sort_by": sort_by, "sort_dir": sort_dir, "limit": limit, "offset": offset,
        }
        return self._raise(self.session.post(
            f"{self.base_url}/api/fr/accords/search", json=payload, timeout=60))

    def get(self, id_or_numero: str) -> Dict[str, Any]:
        """Un accord par son id DILA (`ACCOTEXT000…`) ou son numéro de dépôt (`T…`)."""
        return self._raise(self.session.get(
            f"{self.base_url}/api/fr/accords/{id_or_numero}", timeout=30))

    def themes(self) -> List[Dict[str, Any]]:
        """Nomenclature des thèmes (code + libellé) pour composer un filtre."""
        res = self._raise(self.session.get(
            f"{self.base_url}/api/fr/accords/themes", timeout=30))
        return res.get("themes", res) if isinstance(res, dict) else res

    def sirens_by_idcc(self, idccs: List[str], limit_per_idcc: int = 1000,
                       **filters: Any) -> List[str]:
        """SIREN distincts couverts par PLUSIEURS conventions collectives.

        Une même entreprise porte souvent 3-4 IDCC (le BTP, typiquement) et l'API
        amont n'accepte qu'un code par requête : sans ce helper, chaque appelant
        réécrit la boucle et la déduplication — 1 094 lignes pour 386 entreprises
        distinctes sur un cas réel. Ordre d'apparition conservé (déterministe).
        """
        seen: Dict[str, None] = {}
        for code in idccs:
            res = self.search(idcc=str(code).strip(), limit=limit_per_idcc, **filters)
            for row in res.get("results", []):
                siret = row.get("siret") or ""
                siren = row.get("siren") or (siret[:9] if len(siret) >= 9 else "")
                if siren:
                    seen.setdefault(siren, None)
        return list(seen)
