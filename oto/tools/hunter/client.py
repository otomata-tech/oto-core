"""
Hunter.io API Client for email finding and verification.

Requires: requests
"""

from typing import Optional, Dict, Any

import requests

from ...config import require_secret

_HTTP_TIMEOUT = (10, 60)  # (connexion, lecture) — jamais d'attente illimitée


class HunterError(RuntimeError):
    """Erreur API Hunter — message amont (`errors[].details`) remonté tel quel.

    ⚠️ Hunter **inverse** la convention habituelle :
    - **403** = limite de DÉBIT atteinte → transitoire, réessayer plus tard.
    - **429** = limite d'USAGE du plan (crédits du mois) → **définitif** sur la
      période, réessayer ne sert à rien.

    D'où `retryable`, lu par l'appelant : marteler un 429 Hunter est une perte
    sèche (vécu — 4 appels espacés sur 15 min, quota jamais libéré).
    """

    def __init__(self, message: str, status_code: Optional[int] = None,
                 retryable: bool = False):
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


class HunterClient:
    """
    Hunter.io API client for:
    - Domain email search
    - Email finding by name
    - Email verification
    """

    BASE_URL = "https://api.hunter.io/v2"

    def __init__(self, api_key: str = None):
        """
        Initialize Hunter client.

        Args:
            api_key: Hunter API key (or set HUNTER_API_KEY env var)
        """
        self.api_key = api_key or require_secret("HUNTER_API_KEY")

    @staticmethod
    def _upstream_message(response: requests.Response) -> str:
        """`errors[].details` de Hunter, sinon un extrait du corps brut."""
        try:
            body = response.json()
        except ValueError:
            return (response.text or "").strip()[:400]
        errors = body.get("errors") if isinstance(body, dict) else None
        if isinstance(errors, list) and errors:
            parts = [str(e.get("details") or e.get("id") or e)
                     for e in errors if isinstance(e, dict)]
            if parts:
                return " ; ".join(parts)
        return str(body)[:400]

    def _request(self, endpoint: str, params: Dict = None) -> Dict[str, Any]:
        """Make API request. La clé part en **header** (jamais en query string :
        elle atterrirait dans le message de toute exception). Une erreur HTTP lève
        `HunterError` portant le message amont et la bonne sémantique de réessai."""
        url = f"{self.BASE_URL}/{endpoint}"

        response = requests.get(
            url, params=params or {},
            headers={"Authorization": f"Bearer {self.api_key}"}, timeout=_HTTP_TIMEOUT)
        if not response.ok:
            detail = self._upstream_message(response)
            if response.status_code == 429:
                raise HunterError(
                    f"Hunter — limite d'USAGE du plan atteinte (crédits épuisés "
                    f"sur la période) : {detail}. Réessayer ne libérera rien : "
                    f"bascule sur un autre enrichisseur ou fais monter le plan.",
                    status_code=429, retryable=False)
            if response.status_code == 403:
                raise HunterError(
                    f"Hunter — limite de débit atteinte : {detail}. Espace les "
                    f"appels puis réessaie.", status_code=403, retryable=True)
            raise HunterError(f"Hunter {response.status_code} sur {endpoint} : {detail}",
                              status_code=response.status_code)
        return response.json()

    def domain_search(self, domain: str, limit: int = 10) -> Dict[str, Any]:
        """
        Search for emails on a domain.

        Args:
            domain: Domain to search
            limit: Max results (1 credit per 10 emails)

        Returns:
            Emails found for domain
        """
        return self._request("domain-search", {"domain": domain, "limit": limit})

    def email_finder(
        self,
        domain: str,
        first_name: str = None,
        last_name: str = None,
        full_name: str = None,
    ) -> Dict[str, Any]:
        """
        Find email for a specific person.

        Args:
            domain: Company domain
            first_name: Person's first name
            last_name: Person's last name
            full_name: Full name (alternative to first/last)

        Returns:
            Email and confidence score (1 credit)
        """
        params = {"domain": domain}
        if first_name:
            params["first_name"] = first_name
        if last_name:
            params["last_name"] = last_name
        if full_name:
            params["full_name"] = full_name

        return self._request("email-finder", params)

    def email_verifier(self, email: str) -> Dict[str, Any]:
        """
        Verify an email address.

        Args:
            email: Email to verify

        Returns:
            Verification status (1 credit)
        """
        return self._request("email-verifier", {"email": email})

    def account_info(self) -> Dict[str, Any]:
        """
        Get account info with remaining credits.

        Returns:
            Account details and credits
        """
        return self._request("account")
