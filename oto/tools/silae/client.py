"""
Silae Paie REST API Client — French payroll data.

Docs: https://silae-api.document360.io/docs/authentification

Auth is OAuth2 client-credentials (Azure AD B2C). Three secrets are needed:
  - SILAE_CLIENT_ID       : API account ClientID
  - SILAE_CLIENT_SECRET   : matching ClientSecret
  - SILAE_SUBSCRIPTION_KEY : the "API configuration access key"
                            (sent as Ocp-Apim-Subscription-Key — it scopes
                            which dossiers/functions are reachable).

The Silae API is RPC-flavoured: every function is a POST to
`/v1/<Resource>/<Function>` with a JSON body, the access token in the
`Authorization` header and the subscription key in `Ocp-Apim-Subscription-Key`.

Requires: requests

Usage:
    client = SilaeClient()

    # List the dossiers (payroll files) reachable with this subscription key
    dossiers = client.list_dossiers()

    # List the employees of a dossier
    salaries = client.list_salaries(numero_dossier="001")

    # Fetch a payslip header
    entete = client.bulletin_entete(numero_dossier="001",
                                    matricule_salarie="0001",
                                    periode="2026-05")

    # Push a variable payroll element (heures, prime…)
    client.ajouter_prime(numero_dossier="001", matricule_salarie="0001",
                         code="PRIME", montant=150.0)

    # Redact sensitive fields before they reach the agent (mask IBANs,
    # anonymize names) — see oto.tools.common.FieldFilter. Pass one in, or
    # let from_config("silae") pick up a ~/.otomata/config.yaml policy:
    from oto.tools.common import FieldFilter
    client = SilaeClient(field_filter=FieldFilter(rules=[
        {"fields": ["iban", "bic"], "action": "mask", "keep_last": 4},
        {"fields": ["nom", "prenom"], "action": "anonymize"},
    ]))
"""

import time
from typing import Any, Optional

import requests

from ...config import require_secret, get_cache_dir
from ..common import FieldFilter


class SilaeClient:
    """Client for the Silae Paie REST API (v1)."""

    AUTH_URL = "https://payroll-api-auth.silae.fr/oauth2/v2.0/token"
    BASE_URL = "https://payroll-api.silae.fr/payroll"
    # Fixed Azure AD B2C scope for the payroll API (from Silae docs).
    SCOPE = (
        "https://silaecloudb2c.onmicrosoft.com/"
        "36658aca-9556-41b7-9e48-77e90b006f34/.default"
    )

    def __init__(
        self,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        subscription_key: Optional[str] = None,
        rate_limit_delay: float = 0.2,
        field_filter: Optional[FieldFilter] = None,
    ):
        """
        Initialize the Silae client.

        Args:
            client_id: OAuth ClientID (or SILAE_CLIENT_ID).
            client_secret: OAuth ClientSecret (or SILAE_CLIENT_SECRET).
            subscription_key: API configuration access key, sent as
                Ocp-Apim-Subscription-Key (or SILAE_SUBSCRIPTION_KEY).
            rate_limit_delay: Delay between requests in batch helpers.
            field_filter: Redacts sensitive fields (IBAN, names…) from every
                response. Defaults to the `field_filters.silae` policy in
                ~/.otomata/config.yaml (no-op when none is configured).
        """
        self.client_id = client_id or require_secret("SILAE_CLIENT_ID")
        self.client_secret = client_secret or require_secret("SILAE_CLIENT_SECRET")
        self.subscription_key = subscription_key or require_secret(
            "SILAE_SUBSCRIPTION_KEY"
        )
        self.rate_limit_delay = rate_limit_delay
        self.field_filter = field_filter or FieldFilter.from_config("silae")
        self.session = requests.Session()
        self._token: Optional[str] = None
        self._token_expires_at: float = 0.0
        # Token is reusable for ~60 min — cache it on disk so short-lived
        # client instances don't burn the 60-tokens/minute auth budget.
        self._token_path = get_cache_dir() / "silae-access-token.json"

    # --- Auth ---

    def _get_access_token(self) -> str:
        """Return a valid bearer token, minting/refreshing as needed."""
        now = time.time()
        if self._token and self._token_expires_at > now + 60:
            return self._token

        # Disk cache (shared across instances, keyed by client_id).
        if self._token_path.exists():
            import json

            try:
                data = json.loads(self._token_path.read_text())
            except (ValueError, OSError):
                data = {}
            if (
                data.get("client_id") == self.client_id
                and data.get("expires_at", 0) > now + 60
            ):
                self._token = data["access_token"]
                self._token_expires_at = data["expires_at"]
                return self._token

        resp = self.session.post(
            self.AUTH_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "scope": self.SCOPE,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        )
        resp.raise_for_status()
        token_data = resp.json()

        self._token = token_data["access_token"]
        self._token_expires_at = now + int(token_data.get("expires_in", 3600))

        import json

        self._token_path.parent.mkdir(parents=True, exist_ok=True)
        self._token_path.write_text(
            json.dumps(
                {
                    "client_id": self.client_id,
                    "access_token": self._token,
                    "expires_at": self._token_expires_at,
                }
            )
        )
        return self._token

    def _invalidate_token(self):
        """Drop the cached token to force a refresh on the next call."""
        self._token = None
        self._token_expires_at = 0.0
        self._token_path.unlink(missing_ok=True)

    # --- HTTP ---

    def call(
        self,
        path: str,
        body: Optional[dict] = None,
        numero_dossier: Optional[str] = None,
        method: str = "POST",
        retries: int = 3,
    ) -> Any:
        """
        Call a Silae API function.

        Args:
            path: Function path under the base URL, e.g.
                "v1/SalarieEmplois/ListeSalarieEmplois".
            body: JSON body for the request.
            numero_dossier: When set, sent as the `dossiers` header (some
                functions scope on the folder via the header rather than body).
            method: HTTP method (Silae functions are almost all POST).
            retries: Retries on 401 (token refresh) and 429 (rate limit).

        Returns:
            Parsed JSON response, or an {"error": ...} dict on failure.
        """
        url = f"{self.BASE_URL}/{path.lstrip('/')}"

        for attempt in range(retries):
            token = self._get_access_token()
            headers = {
                "Authorization": f"Bearer {token}",
                "Ocp-Apim-Subscription-Key": self.subscription_key,
                "Accept": "application/json",
            }
            if numero_dossier is not None:
                headers["dossiers"] = str(numero_dossier)

            try:
                resp = self.session.request(
                    method, url, json=body, headers=headers, timeout=60
                )
            except Exception as e:
                return {"error": str(e)}

            # Token expired — refresh once and retry.
            if resp.status_code == 401 and attempt == 0:
                self._invalidate_token()
                continue

            if resp.status_code == 429:
                time.sleep(2 ** attempt)
                continue

            if not resp.ok:
                return {
                    "error": str(resp.status_code),
                    "details": resp.text,
                    "status_code": resp.status_code,
                }

            if resp.status_code == 204 or not resp.content:
                return {"ok": True}

            try:
                parsed = resp.json()
            except ValueError:
                return {"ok": True, "raw": resp.text}

            # Redact sensitive fields before the payload leaves the client.
            return self.field_filter.apply(parsed)

        return {"error": "Max retries exceeded"}

    # --- Dossiers (payroll files) ---

    def list_dossiers(self) -> Any:
        """List the dossiers (payroll files) reachable with this key."""
        return self.call("v1/Dossiers/ListeDossiers", {})

    def list_numeros_dossiers(self) -> Any:
        """List just the dossier numbers."""
        return self.call("v1/Dossiers/ListeNumerosDossiers", {})

    def dossier_infos(self, numero_dossier: str) -> Any:
        """Detailed information for one or more dossiers."""
        return self.call(
            "v1/Dossiers/ListeInformationsDossiersPaie",
            {"numeroDossier": numero_dossier},
            numero_dossier=numero_dossier,
        )

    def dossier_periode_en_cours(self, numero_dossier: str) -> Any:
        """Current open payroll period for a dossier."""
        return self.call(
            "v1/Dossiers/DossierRecupererPeriodeEnCours",
            {"numeroDossier": numero_dossier},
            numero_dossier=numero_dossier,
        )

    # --- Salariés (employees) ---

    def list_salaries(self, numero_dossier: str) -> Any:
        """List the employees of a dossier."""
        return self.call(
            "v1/Salaries/ListeSalaries",
            {"numeroDossier": numero_dossier},
            numero_dossier=numero_dossier,
        )

    def list_salarie_emplois(
        self, numero_dossier: str, matricule_salarie: str = "", type_emplois: int = 0
    ) -> Any:
        """
        List an employee's jobs/positions (emplois).

        Args:
            numero_dossier: Folder number.
            matricule_salarie: Employee registration number (empty = all).
            type_emplois: 0 = current jobs only, 1 = current + archived.
        """
        return self.call(
            "v1/SalarieEmplois/ListeSalarieEmplois",
            {
                "typeEmplois": type_emplois,
                "matriculeSalarie": matricule_salarie,
                "numeroDossier": numero_dossier,
            },
            numero_dossier=numero_dossier,
        )

    def salarie_matricule(self, numero_dossier: str, matricule_salarie: str) -> Any:
        """Fetch an employee by registration number (matricule)."""
        return self.call(
            "v1/Salaries/MatriculeSalarie",
            {
                "numeroDossier": numero_dossier,
                "matriculeSalarie": matricule_salarie,
            },
            numero_dossier=numero_dossier,
        )

    # --- Bulletins (payslips) ---

    def bulletins(
        self, numero_dossier: str, periode: str, matricule_salarie: str = ""
    ) -> Any:
        """
        Retrieve payslips for a period (one employee or the whole dossier).

        Args:
            numero_dossier: Folder number.
            periode: Payroll period (e.g. "2026-05").
            matricule_salarie: Employee matricule, or empty for all employees.
        """
        return self.call(
            "v1/Bulletins/SalariesBulletins",
            {
                "numeroDossier": numero_dossier,
                "periode": periode,
                "matriculeSalarie": matricule_salarie,
            },
            numero_dossier=numero_dossier,
        )

    def bulletin_entete(
        self, numero_dossier: str, matricule_salarie: str, periode: str
    ) -> Any:
        """Payslip header (entête) for one employee/period."""
        return self.call(
            "v1/Bulletins/SalarieBulletinEntete",
            {
                "numeroDossier": numero_dossier,
                "matriculeSalarie": matricule_salarie,
                "periode": periode,
            },
            numero_dossier=numero_dossier,
        )

    def bulletin_lignes(
        self, numero_dossier: str, matricule_salarie: str, periode: str
    ) -> Any:
        """Payslip lines (lignes) for one employee/period."""
        return self.call(
            "v1/Bulletins/SalarieBulletinLignes",
            {
                "numeroDossier": numero_dossier,
                "matriculeSalarie": matricule_salarie,
                "periode": periode,
            },
            numero_dossier=numero_dossier,
        )

    def bulletin_cumuls(
        self, numero_dossier: str, matricule_salarie: str, periode: str
    ) -> Any:
        """Payslip cumulative totals (cumuls) for one employee/period."""
        return self.call(
            "v1/Bulletins/SalarieBulletinCumuls",
            {
                "numeroDossier": numero_dossier,
                "matriculeSalarie": matricule_salarie,
                "periode": periode,
            },
            numero_dossier=numero_dossier,
        )

    # --- Variables de paie (EVP) ---

    def list_variables_a_saisir(self, numero_dossier: str) -> Any:
        """List the payroll variables (EVP) still awaiting entry."""
        return self.call(
            "v1/Variables/ListeVariablesASaisir",
            {"numeroDossier": numero_dossier},
            numero_dossier=numero_dossier,
        )

    def ajouter_element_variable(
        self,
        numero_dossier: str,
        matricule_salarie: str,
        code: str,
        valeur: float,
        **extra,
    ) -> Any:
        """
        Add a variable payroll element (élément variable) to an employee.

        Args:
            numero_dossier: Folder number.
            matricule_salarie: Employee matricule.
            code: Variable/rubrique code.
            valeur: Value to enter.
            **extra: Extra fields passed through to the body (date, unite…).
        """
        body = {
            "numeroDossier": numero_dossier,
            "matriculeSalarie": matricule_salarie,
            "code": code,
            "valeur": valeur,
            **extra,
        }
        return self.call(
            "v1/Variables/SalarieAjouterElementVariable",
            body,
            numero_dossier=numero_dossier,
        )

    def ajouter_prime(
        self,
        numero_dossier: str,
        matricule_salarie: str,
        code: str,
        montant: float,
        **extra,
    ) -> Any:
        """Add a bonus/premium (prime) to an employee."""
        body = {
            "numeroDossier": numero_dossier,
            "matriculeSalarie": matricule_salarie,
            "code": code,
            "montant": montant,
            **extra,
        }
        return self.call(
            "v1/Variables/SalarieAjouterPrime", body, numero_dossier=numero_dossier
        )

    def ajouter_heures(
        self,
        numero_dossier: str,
        matricule_salarie: str,
        code: str,
        nombre: float,
        **extra,
    ) -> Any:
        """Record hours (heures) for an employee."""
        body = {
            "numeroDossier": numero_dossier,
            "matriculeSalarie": matricule_salarie,
            "code": code,
            "nombre": nombre,
            **extra,
        }
        return self.call(
            "v1/Variables/SalarieAjouterHeures", body, numero_dossier=numero_dossier
        )

    def confirmer_saisies(self, numero_dossier: str) -> Any:
        """Confirm (validate) the variable entries staged for a dossier."""
        return self.call(
            "v1/Variables/SalariesConfirmerSaisies",
            {"numeroDossier": numero_dossier},
            numero_dossier=numero_dossier,
        )
