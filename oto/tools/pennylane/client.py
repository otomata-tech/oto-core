"""
Pennylane API Client - Fetch accounting data from Pennylane.

Requires: requests

Usage:
    client = PennylaneClient(api_key="your-api-key")

    # Get company info
    me = client.fetch("me")

    # Get trial balance for a year
    trial = client.fetch("trial_balance", {
        'period_start': '2025-01-01',
        'period_end': '2025-12-31'
    })

    # Fetch all pages of ledger accounts
    accounts = client.fetch_all_pages("ledger_accounts")
"""

import io
import json
import time
from typing import Optional

import requests

from ...config import require_secret
from ..common import FieldFilter, UpstreamHTTPError


def _is_outstanding(transaction) -> bool:
    """True si la transaction porte un `outstanding_balance` non nul (reste à
    lettrer). Champ absent ou illisible → True (conservée : filtrer sur un champ
    douteux ne doit pas faire disparaître de la donnée en silence)."""
    if not isinstance(transaction, dict):
        return True
    value = transaction.get("outstanding_balance")
    if value is None:
        return True
    try:
        return float(str(value)) != 0.0
    except (TypeError, ValueError):
        return True


class PennylaneClient:
    """Client for Pennylane API v2"""

    BASE_URL = "https://app.pennylane.com/api/external/v2"

    def __init__(self, api_key: str = None, rate_limit_delay: float = 0.3,
                 field_filter: "FieldFilter" = None):
        """
        Initialize the Pennylane client.

        Args:
            api_key: Pennylane API bearer token (or set PENNYLANE_API_KEY env var)
            rate_limit_delay: Delay between requests (default 0.3s for 4 req/sec limit)
            field_filter: Redacts sensitive fields (IBAN, names…) from every
                response. Defaults to the `field_filters.pennylane` policy in
                ~/.otomata/config.yaml (no-op when none is configured).
        """
        self.api_key = api_key or require_secret("PENNYLANE_API_KEY")
        self.rate_limit_delay = rate_limit_delay
        self.field_filter = field_filter or FieldFilter.from_config("pennylane")
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
        })

    def post(self, endpoint: str, data: dict, retries: int = 3) -> dict:
        """POST data to Pennylane API with retry on rate limit."""
        url = f"{self.BASE_URL}/{endpoint}"

        for attempt in range(retries):
            try:
                response = self.session.post(url, json=data, timeout=30)

                if response.status_code == 429:
                    wait_time = 2 ** attempt
                    time.sleep(wait_time)
                    continue

                if not response.ok:
                    return {
                        "error": str(response.status_code),
                        "details": response.text,
                        "status_code": response.status_code,
                    }

                if response.status_code == 204 or not response.content:
                    return {"ok": True}

                return self.field_filter.apply(response.json())
            except Exception as e:
                return {"error": str(e)}

        return {"error": "Max retries exceeded"}

    def put(self, endpoint: str, data: dict, retries: int = 3) -> dict:
        """PUT data to Pennylane API with retry on rate limit."""
        url = f"{self.BASE_URL}/{endpoint}"

        for attempt in range(retries):
            try:
                response = self.session.put(url, json=data, timeout=30)

                if response.status_code == 429:
                    wait_time = 2 ** attempt
                    time.sleep(wait_time)
                    continue

                if not response.ok:
                    return {
                        "error": str(response.status_code),
                        "details": response.text,
                        "status_code": response.status_code,
                    }

                return self.field_filter.apply(response.json())
            except Exception as e:
                return {"error": str(e)}

        return {"error": "Max retries exceeded"}

    def delete(self, endpoint: str, retries: int = 3) -> dict:
        """DELETE a resource on Pennylane API with retry on rate limit."""
        url = f"{self.BASE_URL}/{endpoint}"

        for attempt in range(retries):
            try:
                response = self.session.delete(url, timeout=30)

                if response.status_code == 429:
                    wait_time = 2 ** attempt
                    time.sleep(wait_time)
                    continue

                if not response.ok:
                    return {
                        "error": str(response.status_code),
                        "details": response.text,
                        "status_code": response.status_code,
                    }

                if response.status_code == 204 or not response.content:
                    return {"ok": True}

                return self.field_filter.apply(response.json())
            except Exception as e:
                return {"error": str(e)}

        return {"error": "Max retries exceeded"}

    def fetch(self, endpoint: str, params: Optional[dict] = None, retries: int = 3) -> dict:
        """
        Fetch data from Pennylane API with retry on rate limit.

        Args:
            endpoint: API endpoint (e.g., "me", "trial_balance", "ledger_accounts")
            params: Optional query parameters
            retries: Number of retries on rate limit

        Returns:
            JSON response as dict
        """
        url = f"{self.BASE_URL}/{endpoint}"

        for attempt in range(retries):
            try:
                response = self.session.get(url, params=params, timeout=30)

                if response.status_code == 429:  # Rate limited
                    wait_time = 2 ** attempt
                    time.sleep(wait_time)
                    continue

                if not response.ok:
                    return {
                        "error": str(response.status_code),
                        "details": response.text,
                        "status_code": response.status_code,
                    }

                return self.field_filter.apply(response.json())
            except Exception as e:
                return {"error": str(e)}

        return {"error": "Max retries exceeded"}

    def fetch_all_pages(
        self,
        endpoint: str,
        params: Optional[dict] = None,
        max_pages: Optional[int] = None,
        per_page: int = 100
    ) -> list:
        """
        Fetch all pages of a paginated endpoint (cursor pagination).

        API Pennylane 2026 : seule la pagination par **curseur** (`cursor` + `limit`)
        est supportée ; les anciens `page`/`per_page` renvoient HTTP 400. La réponse
        porte `items`, `has_more` et `next_cursor` — on repasse `next_cursor` dans
        `cursor` pour la page suivante. `max_pages` borne le nombre d'itérations,
        `per_page` est envoyé comme `limit` (max 100).
        """
        all_data = []
        if params is None:
            params = {}
        params = dict(params)
        params['limit'] = min(per_page, 100)
        params.pop('page', None)
        params.pop('per_page', None)
        cursor = None
        pages = 0

        while True:
            if cursor:
                params['cursor'] = cursor

            data = self.fetch(endpoint, params)
            time.sleep(self.rate_limit_delay)

            # Une erreur amont (401 clé périmée, 4xx/5xx, réseau) NE DOIT PAS être
            # avalée en liste vide : sinon un consommateur anti-doublon (ex.
            # find_invoice_by_external_reference) confond « erreur d'auth » et
            # « aucun résultat » et recrée des avoirs en double (oto-backend#223).
            # On lève — UpstreamHTTPError si un status HTTP est porté (le backend
            # le classe en erreur connecteur gérée via `.status_code`), sinon une
            # erreur générique (réseau / max retries) qui remonte tout de même.
            if isinstance(data, dict) and 'error' in data:
                status = data.get('status_code')
                if isinstance(status, int):
                    raise UpstreamHTTPError(status, data.get('details') or data['error'],
                                            service="pennylane")
                raise RuntimeError(f"pennylane: {data['error']}")

            if isinstance(data, dict) and 'items' in data:
                items = data['items']
                has_more = data.get('has_more', False)
                next_cursor = data.get('next_cursor')
            else:
                # endpoint non paginé (renvoie une liste ou un objet brut)
                return data if isinstance(data, list) else [data]

            if items:
                all_data.extend(items)

            pages += 1
            if not has_more or not next_cursor:
                break
            if max_pages and pages >= max_pages:
                break
            cursor = next_cursor

        return all_data

    def get_company_info(self) -> dict:
        """Get company information."""
        return self.fetch("me")

    def get_fiscal_years(self) -> list:
        """Get fiscal years."""
        return self.fetch("fiscal_years")

    def get_trial_balance(self, start_date: str, end_date: str) -> list:
        """
        Get trial balance for a period.

        Args:
            start_date: Period start (YYYY-MM-DD)
            end_date: Period end (YYYY-MM-DD)
        """
        return self.fetch_all_pages("trial_balance", {
            'period_start': start_date,
            'period_end': end_date
        })

    def get_ledger_accounts(self) -> list:
        """Get all ledger accounts."""
        return self.fetch_all_pages("ledger_accounts")

    def get_ledger_entries(self, max_pages: Optional[int] = None) -> list:
        """Get ledger entries."""
        return self.fetch_all_pages("ledger_entries", max_pages=max_pages)

    def get_customer_invoices(self, max_pages: Optional[int] = None) -> list:
        """Get customer invoices."""
        return self.fetch_all_pages("customer_invoices", max_pages=max_pages)

    def get_supplier_invoices(self, max_pages: Optional[int] = None) -> list:
        """Get supplier invoices."""
        return self.fetch_all_pages("supplier_invoices", max_pages=max_pages)

    def get_categories(self) -> list:
        """Get expense categories."""
        return self.fetch("categories")

    def get_transactions(self, max_pages: Optional[int] = None,
                         period_start: Optional[str] = None,
                         period_end: Optional[str] = None,
                         only_outstanding: bool = False,
                         per_page: int = 100) -> list:
        """Get bank transactions, with optional source-side reduction levers.

        Sans levier, l'endpoint renvoie TOUT l'historique (vécu : 307
        transactions ≈ 247k chars — inexploitable par un agent). Les filtres
        sont OPTIONNELS (le brut reste le défaut) :

        Args:
            max_pages: borne le nombre de pages ramenées.
            period_start / period_end: bornes de date (YYYY-MM-DD), filtrées
                CÔTÉ SERVEUR (param `filter` de l'API v2, opérateurs gteq/lteq
                sur `date`) — le volume est réduit à la source.
            only_outstanding: ne garde que les transactions non soldées
                (`outstanding_balance` ≠ 0) — filtre côté client, appliqué aux
                pages ramenées. Un montant absent/illisible est CONSERVÉ
                (on ne perd pas de donnée sur un champ douteux).
            per_page: taille de page (≤100) — affine la granularité de max_pages.
        """
        params: dict = {}
        filters = []
        if period_start:
            filters.append({"field": "date", "operator": "gteq", "value": period_start})
        if period_end:
            filters.append({"field": "date", "operator": "lteq", "value": period_end})
        if filters:
            params["filter"] = json.dumps(filters)
        items = self.fetch_all_pages("transactions", params or None,
                                     max_pages=max_pages, per_page=per_page)
        if only_outstanding:
            items = [t for t in items if _is_outstanding(t)]
        return items

    # --- Matching (lettrage) ---

    def match_transaction(self, invoice_id: int, transaction_id: int,
                          invoice_type: str = "customer") -> dict:
        """Lettre (reconcile) a bank transaction with an invoice.

        Reversible accounting link, not a new entry. invoice_type is
        "customer" (ventes) or "supplier" (achats).
        """
        endpoint = f"{invoice_type}_invoices/{invoice_id}/matched_transactions"
        return self.post(endpoint, {"transaction_id": transaction_id})

    # --- File Attachments ---

    def upload_file(self, file_path: str) -> dict:
        """Upload a file (PDF) to Pennylane. Returns dict with id, filename, url."""
        import os
        filename = os.path.basename(file_path)
        with open(file_path, "rb") as f:
            return self.upload_file_bytes(f.read(), filename)

    def upload_file_bytes(self, data: bytes, filename: str,
                          content_type: str = "application/pdf") -> dict:
        """Upload des OCTETS (PDF) sur Pennylane sans passer par le disque.

        Variante de `upload_file` pour un appelant qui détient déjà les octets
        (fichier « côté oto » : Drive, pièce Gmail, URL — résolus en amont). Poste
        en multipart sur `POST /file_attachments`. Renvoie `{id, filename, url}` ;
        sur erreur `{error, details, status_code}`. L'`id` est le `file_attachment_id`
        à passer à `import_supplier_invoice`.
        """
        url = f"{self.BASE_URL}/file_attachments"
        response = self.session.post(
            url,
            files={"file": (filename, io.BytesIO(data), content_type)},
            timeout=60,
        )
        if not response.ok:
            return {
                "error": str(response.status_code),
                "details": response.text,
                "status_code": response.status_code,
            }
        return response.json()

    def import_supplier_invoice(
        self, file_attachment_id: int, supplier_id: int, date: str, deadline: str,
        currency_amount_before_tax: str, currency_amount: str, currency_tax: str,
        invoice_lines: list[dict], currency: str = "EUR",
        external_reference: Optional[str] = None, import_as_incomplete: bool = False,
        invoice_number: Optional[str] = None, label: Optional[str] = None,
    ) -> dict:
        """Crée une facture FOURNISSEUR à partir d'une pièce déjà uploadée.

        `POST /supplier_invoices/import` : lie le `file_attachment_id` (cf.
        `upload_file_bytes`) à une facture fournisseur en brouillon. Pas d'OCR côté
        Pennylane — l'appelant FOURNIT les champs (lus depuis le PDF) : `supplier_id`,
        `date`/`deadline` (ISO), montants **en string** (`currency_amount_before_tax`,
        `currency_amount`=TTC, `currency_tax`), et `invoice_lines` (≥1). Pennylane
        déduplique par PDF (422 si le même file_attachment est ré-importé) ;
        `external_reference` trace la source et garde l'appelant idempotent.
        """
        body = {
            "file_attachment_id": file_attachment_id,
            "supplier_id": supplier_id,
            "date": date,
            "deadline": deadline,
            "currency": currency,
            "currency_amount_before_tax": currency_amount_before_tax,
            "currency_amount": currency_amount,
            "currency_tax": currency_tax,
            "invoice_lines": invoice_lines,
            "import_as_incomplete": import_as_incomplete,
        }
        if external_reference:
            body["external_reference"] = external_reference
        if invoice_number:
            body["invoice_number"] = invoice_number
        if label:
            body["label"] = label
        return self.post("supplier_invoices/import", body)

    # --- Customers ---

    def list_customers(self, max_pages: Optional[int] = None) -> list:
        """List all customers."""
        return self.fetch_all_pages("company_customers", max_pages=max_pages)

    def create_customer(self, name: str, emails: list[str] = None,
                        address: str = None, postal_code: str = None,
                        city: str = None, country_alpha2: str = "FR",
                        external_reference: str = None) -> dict:
        """Create a customer."""
        body = {"name": name}
        if emails:
            body["emails"] = emails
        if address or postal_code or city:
            body["billing_address"] = {
                k: v for k, v in {
                    "address": address, "postal_code": postal_code,
                    "city": city, "country_alpha2": country_alpha2,
                }.items() if v
            }
        if external_reference:
            body["external_reference"] = external_reference
        return self.post("company_customers", body)

    def update_customer(self, customer_id: int, **fields) -> dict:
        """Update a customer. Accepts any top-level field (name, vat_number, emails, billing_address, etc.)."""
        return self.put(f"company_customers/{customer_id}", fields)

    # --- Suppliers ---
    # NB: unlike customers (`company_customers`), the v2 supplier resource is `suppliers`
    # (GET/POST /suppliers, GET/PUT /suppliers/{id}). Needed to create/reuse a supplier
    # before `import_supplier_invoice` (which requires an existing supplier_id).

    def list_suppliers(self, max_pages: Optional[int] = None) -> list:
        """List all suppliers (id + name → resolve a supplier_id from a name)."""
        return self.fetch_all_pages("suppliers", max_pages=max_pages)

    def create_supplier(self, name: str, **fields) -> dict:
        """Create a supplier. `name` is required; pass any other documented top-level
        field (vat_number, reg_no, emails, address, iban, external_reference…)."""
        return self.post("suppliers", {"name": name, **fields})

    def get_supplier(self, supplier_id: int) -> dict:
        """Retrieve a single supplier by id."""
        return self.fetch(f"suppliers/{supplier_id}")

    # --- Products ---

    def list_products(self, max_pages: Optional[int] = None) -> list:
        """List all products."""
        return self.fetch_all_pages("products", max_pages=max_pages)

    def get_product(self, product_id: int) -> dict:
        """One product by id."""
        return self.fetch(f"products/{product_id}")

    def create_product(self, label: str, unit_price: str, unit: str = "day",
                       vat_rate: str = "FR_200", description: str = None) -> dict:
        """Create a product. unit_price as string (e.g. '700.00')."""
        body = {
            "label": label,
            "price_before_tax": unit_price,
            "unit": unit,
            "vat_rate": vat_rate,
        }
        if description:
            body["description"] = description
        return self.post("products", body)

    # --- Customer Invoices ---

    def create_customer_invoice(self, customer_id: int, date: str, deadline: str,
                                lines: list[dict], draft: bool = True,
                                external_reference: str = None,
                                pdf_free_text: str = None,
                                currency: str = "EUR") -> dict:
        """
        Create a customer invoice.

        lines: list of dicts with keys: product_id, quantity, and optionally
               label, raw_currency_unit_price, unit, vat_rate.
        pdf_free_text: free text printed on the PDF (customer-visible comment,
               API field `pdf_invoice_free_text`).
        """
        body = {
            "customer_id": customer_id,
            "date": date,
            "deadline": deadline,
            "draft": draft,
            "currency": currency,
            "invoice_lines": lines,
        }
        if external_reference:
            body["external_reference"] = external_reference
        if pdf_free_text:
            body["pdf_invoice_free_text"] = pdf_free_text
        return self.post("customer_invoices", body)

    def create_credit_note(self, customer_id: int, date: str, deadline: str,
                           lines: list[dict], external_reference: str = None,
                           pdf_free_text: str = None,
                           draft: bool = True, currency: str = "EUR") -> dict:
        """Create a STANDALONE credit note (avoir) — an invoice with negative amounts.

        Official v2 convention (changelog « V2 - List Credit Notes and Customer
        Invoices ») : there is no dedicated credit-note endpoint — an avoir IS a
        `customer_invoice` whose amounts are NEGATIVE. The caller provides
        POSITIVE business lines (e.g. 195 credits at 1.45) ; this method flips
        each line's quantity sign so the « avoir » nature is structural, never
        left to the caller.

        NO linking at creation : the create-endpoint attribute
        `credited_invoice_id` is broken upstream (« not working as expected …
        will be removed » — Pennylane changelog). Link afterwards with
        `link_credit_note()` if ever needed ; the MM practice links nothing
        (the AUT-… reference lives in free text on the invoice label).

        `external_reference` traces the source event (e.g. a GoCardless payment
        id `PMxxxx`) and keeps the caller idempotent (one failed payment → one
        avoir). Draft by default — finalize separately after human validation.

        lines: same shape as create_customer_invoice (product_id, quantity, and
               optionally label, raw_currency_unit_price, unit, vat_rate).
        """
        neg_lines = []
        for line in lines:
            li = dict(line)
            qty = li.get("quantity")
            if isinstance(qty, bool) or not isinstance(qty, (int, float)) or qty == 0:
                raise ValueError(
                    "credit-note line needs a non-zero numeric `quantity` "
                    f"(got {qty!r})")
            li["quantity"] = -abs(qty)
            neg_lines.append(li)
        body = {
            "customer_id": customer_id,
            "date": date,
            "deadline": deadline,
            "draft": draft,
            "currency": currency,
            "invoice_lines": neg_lines,
        }
        if external_reference:
            body["external_reference"] = external_reference
        if pdf_free_text:
            body["pdf_invoice_free_text"] = pdf_free_text
        return self.post("customer_invoices", body)

    def link_credit_note(self, invoice_id: int, credit_note_id: int) -> dict:
        """Link an existing credit note to the customer invoice it credits.

        Dedicated v2 endpoint — the only working way to link (the create-time
        `credited_invoice_id` attribute is broken upstream)."""
        return self.post(f"customer_invoices/{invoice_id}/link_credit_note",
                         {"credit_note_id": credit_note_id})

    def find_invoice_by_external_reference(self, external_reference: str,
                                           max_pages: int = 5) -> Optional[dict]:
        """Return the customer invoice carrying this `external_reference`, or None.

        Anti-duplicate guard for credit notes: before creating an avoir for a
        GoCardless payment id, check none already references it. Scans
        customer_invoices (bounded by max_pages) — no documented server-side
        filter on external_reference on API v2 yet, so this is a client-side scan.
        """
        for inv in self.get_customer_invoices(max_pages=max_pages):
            if isinstance(inv, dict) and inv.get("external_reference") == external_reference:
                return inv
        return None

    def update_invoice(self, invoice_id: int, **fields) -> dict:
        """Update a draft invoice. Accepts any field (customer_id, date, deadline, etc.)."""
        return self.put(f"customer_invoices/{invoice_id}", fields)

    def update_invoice_line(self, invoice_id: int, line_id: int, **fields) -> dict:
        """Update a line on a draft invoice (quantity, raw_currency_unit_price, label, ...).

        Pennylane expects Rails-style nested attributes, so a dedicated wrapper
        avoids clients having to know the shape.
        """
        body = {"invoice_lines": {"update": [{"id": line_id, **fields}]}}
        return self.put(f"customer_invoices/{invoice_id}", body)

    def finalize_invoice(self, invoice_id: int) -> dict:
        """Finalize a draft invoice."""
        return self.put(f"customer_invoices/{invoice_id}/finalize", {})

    def delete_invoice(self, invoice_id: int) -> dict:
        """Delete a draft customer invoice. Only drafts can be deleted;
        finalized invoices must be cancelled with a credit note instead."""
        return self.delete(f"customer_invoices/{invoice_id}")

    def send_invoice(self, invoice_id: int) -> dict:
        """Send a finalized invoice to the customer by email (uses customer's email on file)."""
        return self.post(f"customer_invoices/{invoice_id}/send_by_email", {})

    def get_invoice_lines(self, invoice_id: int) -> list:
        """Get the lines of a customer invoice."""
        data = self.fetch(f"customer_invoices/{invoice_id}/invoice_lines")
        return data.get("items", []) if isinstance(data, dict) else []

    # --- Quotes ---

    def create_quote(self, customer_id: int, date: str, deadline: str,
                     lines: list[dict], external_reference: str = None,
                     currency: str = "EUR", language: str = "fr_FR") -> dict:
        """Create a quote."""
        body = {
            "customer_id": customer_id,
            "date": date,
            "deadline": deadline,
            "currency": currency,
            "language": language,
            "invoice_lines": lines,
        }
        if external_reference:
            body["external_reference"] = external_reference
        return self.post("quotes", body)

    # --- Aggregates ---

    def fetch_complete_data(self, year: int = 2025) -> dict:
        """
        Fetch complete financial data for a year.

        Args:
            year: Fiscal year to fetch

        Returns:
            Dict with all financial data
        """
        data = {
            'company': self.get_company_info(),
            'fiscal_years': self.get_fiscal_years(),
            'ledger_accounts': self.get_ledger_accounts(),
            f'trial_balance_{year}': self.get_trial_balance(
                f'{year}-01-01', f'{year}-12-31'
            ),
            'customer_invoices': self.get_customer_invoices(max_pages=50),
            'supplier_invoices': self.get_supplier_invoices(max_pages=50),
            'categories': self.get_categories(),
        }
        return data
