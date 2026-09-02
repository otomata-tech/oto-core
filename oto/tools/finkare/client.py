"""Finkare API client — AI-driven receivables collection (https://docs.finkare.io).

Covers the four documented v1 resources: invoices, debtors, payments and the
collection workflow. Auth is a single `X-API-Key` header (not a bearer token).

⚠️ **The key carries its own environment.** `fk_test_…` keys belong to the sandbox
and `fk_live_…` keys to production — so the base URL is DERIVED from the key rather
than passed alongside it. Two reasons: a test key can never reach production by
accident, and callers cannot mix a live key with a sandbox URL (the pair would be
accepted by neither side, and the error would blame the wrong thing).

⚠️ Amounts are in CENTS everywhere, and every write accepts an `Idempotency-Key`
header — a retried POST must not create a second invoice.
"""
import uuid
from typing import Any, Dict, List, Optional

import requests

from ...config import require_secret
from ..common import raise_for_upstream

# The prefix that marks a sandbox key. Anything else is treated as production —
# failing towards the sandbox on an unknown prefix would silently swallow live work.
_SANDBOX_PREFIX = "fk_test_"

_PROD_BASE = "https://api.finkare.io/api/v1"
_SANDBOX_BASE = "https://api-sandbox.finkare.io/api/v1"


class FinkareClient:
    """Thin wrapper over the Finkare v1 REST API."""

    def __init__(self, api_key: str = None, timeout: int = 30):
        self.api_key = api_key or require_secret("FINKARE_API_KEY")
        self.timeout = timeout
        self.base_url = (_SANDBOX_BASE if self.api_key.startswith(_SANDBOX_PREFIX)
                         else _PROD_BASE)

    @property
    def is_sandbox(self) -> bool:
        return self.base_url == _SANDBOX_BASE

    def _request(self, method: str, endpoint: str, *, idempotent: bool = False,
                 **kwargs) -> Any:
        headers = {"X-API-Key": self.api_key, "Content-Type": "application/json"}
        # Writes get a fresh idempotency key unless the caller pinned one: a retry
        # after a network timeout must not book the same invoice twice.
        if idempotent:
            headers["Idempotency-Key"] = kwargs.pop("idempotency_key", None) or str(
                uuid.uuid4())
        resp = requests.request(method, f"{self.base_url}/{endpoint.lstrip('/')}",
                                headers=headers, timeout=self.timeout, **kwargs)
        raise_for_upstream(resp, service="finkare")
        return resp.json() if resp.content else {}

    # --- Invoices ----------------------------------------------------------
    def create_invoice(self, invoice: Dict[str, Any],
                       idempotency_key: str = None) -> Dict[str, Any]:
        """One receivable. `invoiceNumber`, `amountCents` and `dueDate` are required,
        plus a `debtor` object (name/email, optionally siret/address/city/postalCode)."""
        return self._request("POST", "invoices", json=invoice, idempotent=True,
                             idempotency_key=idempotency_key)

    def create_invoices_bulk(self, invoices: List[Dict[str, Any]],
                             idempotency_key: str = None) -> Dict[str, Any]:
        """Batch import — the API caps a request at 100 invoices and 10 MB."""
        return self._request("POST", "invoices/bulk", json={"invoices": invoices},
                             idempotent=True, idempotency_key=idempotency_key)

    def list_invoices(self, status: str = None, page: int = None,
                      limit: int = None, **params) -> Dict[str, Any]:
        query = {k: v for k, v in
                 {"status": status, "page": page, "limit": limit, **params}.items()
                 if v is not None}
        return self._request("GET", "invoices", params=query)

    def cancel_invoice(self, invoice_id: str, reason: str = None) -> Dict[str, Any]:
        """Stops the collection workflow on this receivable."""
        body = {"reason": reason} if reason else {}
        return self._request("POST", f"invoices/{invoice_id}/cancel", json=body,
                             idempotent=True)

    # --- Debtors -----------------------------------------------------------
    def list_debtors(self, search: str = None, page: int = None,
                     limit: int = None) -> Dict[str, Any]:
        query = {k: v for k, v in
                 {"search": search, "page": page, "limit": limit}.items()
                 if v is not None}
        return self._request("GET", "debtors", params=query)

    def get_debtor(self, debtor_id: str) -> Dict[str, Any]:
        return self._request("GET", f"debtors/{debtor_id}")

    def create_debtor(self, debtor: Dict[str, Any]) -> Dict[str, Any]:
        """`name` and `email` are required; `siret` is 14 digits, `country` ISO-3166
        alpha-2 (defaults to FR server-side)."""
        return self._request("POST", "debtors", json=debtor, idempotent=True)

    def update_debtor(self, debtor_id: str, debtor: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("PUT", f"debtors/{debtor_id}", json=debtor)

    def debtor_invoices(self, debtor_id: str) -> Dict[str, Any]:
        return self._request("GET", f"debtors/{debtor_id}/invoices")

    def debtor_score(self, debtor_id: str) -> Dict[str, Any]:
        """Payment-behaviour score computed by Finkare."""
        return self._request("GET", f"debtors/{debtor_id}/score")

    # --- Payments (read-only) ----------------------------------------------
    def list_payments(self, status: str = None, from_date: str = None,
                      to_date: str = None, invoice_id: str = None,
                      page: int = None, limit: int = None) -> Dict[str, Any]:
        query = {k: v for k, v in {"status": status, "fromDate": from_date,
                                   "toDate": to_date, "invoiceId": invoice_id,
                                   "page": page, "limit": limit}.items()
                 if v is not None}
        return self._request("GET", "payments", params=query)

    def get_payment(self, payment_id: str) -> Dict[str, Any]:
        return self._request("GET", f"payments/{payment_id}")

    def invoice_payments(self, invoice_id: str) -> Dict[str, Any]:
        return self._request("GET", f"payments/invoice/{invoice_id}")

    def payment_stats(self, period: str = None) -> Dict[str, Any]:
        """day|week|month|year — needs the `reports:read` scope, not `payments:read`."""
        query = {"period": period} if period else {}
        return self._request("GET", "payments/stats/summary", params=query)

    # --- Collection workflow ------------------------------------------------
    def workflow_status(self, invoice_id: str) -> Dict[str, Any]:
        return self._request("GET", f"workflow/invoice/{invoice_id}")

    def workflow_history(self, invoice_id: str) -> Dict[str, Any]:
        return self._request("GET", f"workflow/invoice/{invoice_id}/history")

    def workflow_next_action(self, invoice_id: str) -> Dict[str, Any]:
        return self._request("GET", f"workflow/invoice/{invoice_id}/next-action")

    def workflow_trigger(self, invoice_id: str, action: str,
                         reason: str = None) -> Dict[str, Any]:
        """action = start|pause|resume|cancel|escalate. `reason` is free text kept
        for traceability — worth filling on pause/cancel/escalate."""
        body: Dict[str, Any] = {"action": action}
        if reason:
            body["reason"] = reason
        return self._request("POST", f"workflow/invoice/{invoice_id}/trigger",
                             json=body, idempotent=True)

    def workflow_stats(self) -> Dict[str, Any]:
        return self._request("GET", "workflow/stats")
