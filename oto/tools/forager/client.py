"""Forager.ai API Client — https://docs.forager.ai (job posts, org/people enrichment)

**Live-tested 2026-08-21** against a real Trial-tier key (50.00 credits,
36.00 spent across one call per method — see below for confirmed costs and
behavior). Everything else in this docstring was spec-only until then.

Paid, per-lookup API — most calls here bill credits against the account's
subscription; confirmed free: `get_current_user`, `list_balance_change_logs`,
`get_balance_change_totals`, every `autocomplete_*`, both `*_totals` search
methods, and — **only when they return no result** — the contact-lookup
methods (`lookup_person_work_emails`/`personal_emails`/`phone_numbers`
return `[]` and are NOT billed on a miss; billed only on an actual hit,
confirmed live: `phone_numbers` billed 15.00 on a hit, `work_emails`/
`personal_emails` billed nothing on `[]`).

⚠️ **Two different "no match" behaviors, confirmed live — do not conflate
them.** `lookup_person_work_emails`/`personal_emails`/`phone_numbers`
degrade to an empty list `[]` on no match (see above). But
`lookup_person_by_email`/`lookup_person_by_phone_number` (the REVERSE
lookups) raise `UpstreamHTTPError(404)` instead — confirmed live
(`{'detail': 'No person matches the given email.'}`), no charge either way.
A caller checking "did this find anything" needs a different check per
method family: falsy return vs. caught exception.

⚠️ **`search_job_posts_totals`'s count does not necessarily match
`search_job_posts`'s `total_search_results` for the identical filter** —
confirmed live on one real query (500 vs. 949). Not investigated further
(could be a Forager-side inconsistency, not something this client can fix);
treat `_totals` as the number to trust for volume, not a cross-check.

**IDs, not free text.** Filters like `locations`, `industries`,
`industries_exclude`, `keywords`/`organization_keywords`, `web_technologies`/
`organization_web_technologies` and `person_skills` all take **integer IDs**
resolved via the `autocomplete_*` methods below — passing a raw string
(e.g. `locations=["Paris"]`) will not match anything. ⚠️ **Confirmed live:
`autocomplete_*` returns `results[].id` as a STRING** (e.g. `"9839"`), even
though every filter field expecting it is typed `[integer]` — cast to `int`
before passing into a search filter. Not confirmed whether the search
endpoints tolerate the string form directly (not worth spending more trial
credits to check); casting defensively is free and safe either way.

**No API-key management here, deliberately.** The spec exposes
`GET/POST /api/api_keys/` and `GET/DELETE /api/api_keys/{prefix}/`, but this
client does not wrap them: creating a key is a secret-issuing operation and
deleting one can silently break another integration — both are dashboard-only
by this codebase's convention, not something a client (and later an agent)
should be able to trigger. Manage keys at https://app.forager.ai.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import requests

from ...config import require_secret
from ..common import raise_for_upstream

_HTTP_TIMEOUT = (10, 60)  # (connexion, lecture) — jamais d'attente illimitée


class ForagerClient:
    BASE_URL = "https://api-v2.forager.ai"

    def __init__(self, api_key: str = None, account_id: Optional[int] = None):
        self.api_key = api_key or require_secret("FORAGER_API_KEY")
        self._account_id = int(account_id) if account_id else None

    def _headers(self) -> Dict[str, str]:
        return {"X-API-KEY": self.api_key}

    def _request(self, method: str, path: str, *, json: Any = None, params: Dict = None) -> Any:
        resp = requests.request(
            method, f"{self.BASE_URL}{path}", headers=self._headers(), json=json, params=params
        , timeout=_HTTP_TIMEOUT)
        raise_for_upstream(resp, service="forager")
        return resp.json() if resp.content else None

    # ------------------------------------------------------------------
    # Users / account

    def get_current_user(self) -> Dict[str, Any]:
        """`GET /api/users/current/` — identity + per-account subscription/credits. Free."""
        return self._request("GET", "/api/users/current/")

    def resolve_account_id(self) -> int:
        """Resolve the `account_id` required by every datastorage/subscriptions call.

        Uses the `account_id` passed at construction if given. Otherwise calls
        `get_current_user()` and expects exactly one entry in `accounts[]`.
        A key with access to more than one account and no `account_id` passed
        explicitly is a refusal, not a guess — picking the wrong account bills
        someone else's credits.
        """
        if self._account_id is not None:
            return self._account_id
        me = self.get_current_user()
        accounts = me.get("accounts") or []
        if not accounts:
            raise ValueError("forager: this API key has access to no account")
        if len(accounts) > 1:
            listing = ", ".join(f"{a['id']} ({a.get('name')})" for a in accounts)
            raise ValueError(
                f"forager: this API key has access to multiple accounts ({listing}) — "
                "pass account_id explicitly, refusing to guess which one to bill"
            )
        self._account_id = accounts[0]["id"]
        return self._account_id

    def _account_path(self, suffix: str) -> str:
        return f"/api/{self.resolve_account_id()}{suffix}"

    # ------------------------------------------------------------------
    # Feedback — reports a lookup's contact as correct/incorrect. Every call
    # creates a NEW row (201), never an upsert.

    def submit_personal_email_feedback(
        self, email: str, contact_status: str, is_correct_person: bool,
        *, name: str = None, person_id: int = None,
    ) -> Dict[str, Any]:
        """contact_status: 'valid' | 'invalid'."""
        body = {"email": email, "contact_status": contact_status, "is_correct_person": is_correct_person}
        if name is not None:
            body["name"] = name
        if person_id is not None:
            body["person_id"] = person_id
        return self._request("POST", self._account_path("/datastorage/feedback/personal_emails/"), json=body)

    def submit_phone_number_feedback(
        self, phone_number: str, contact_status: str, is_correct_person: bool,
        *, name: str = None, person_id: int = None,
    ) -> Dict[str, Any]:
        """contact_status: 'connected' | 'disconnected'."""
        body = {"phone_number": phone_number, "contact_status": contact_status, "is_correct_person": is_correct_person}
        if name is not None:
            body["name"] = name
        if person_id is not None:
            body["person_id"] = person_id
        return self._request("POST", self._account_path("/datastorage/feedback/phone_numbers/"), json=body)

    def submit_work_email_feedback(
        self, email: str, contact_status: str, is_correct_person: bool,
        *, name: str = None, person_id: int = None,
    ) -> Dict[str, Any]:
        """contact_status: 'valid' | 'invalid'."""
        body = {"email": email, "contact_status": contact_status, "is_correct_person": is_correct_person}
        if name is not None:
            body["name"] = name
        if person_id is not None:
            body["person_id"] = person_id
        return self._request("POST", self._account_path("/datastorage/feedback/work_emails/"), json=body)

    # ------------------------------------------------------------------
    # Job posts. Filters (all optional, via **filters): page, job_source
    # ('indeed'|'linkedin'|'angellist'), date_featured_start/end,
    # organization_ids ([int]), title, description, is_remote, is_active,
    # locations/locations_exclude ([int] — resolve via autocomplete_locations).
    # Bills a credit per call; check totals first when only a count is needed.

    def search_job_posts(self, **filters: Any) -> Dict[str, Any]:
        return self._request("POST", self._account_path("/datastorage/job_search/"), json=filters)

    def search_job_posts_totals(self, **filters: Any) -> Dict[str, Any]:
        return self._request("POST", self._account_path("/datastorage/job_search/totals/"), json=filters)

    # ------------------------------------------------------------------
    # Organizations. Filters (all optional, via **filters): page,
    # organization_ids ([int]), description, locations/industries/
    # industries_exclude/web_technologies ([int] IDs — via autocomplete),
    # keywords ([int] — via autocomplete_organization_keywords),
    # employees_start/end, founded_date_start/end, revenue_start/end,
    # domains ([str]), domain_rank_start/end, domain_traffic_start/end,
    # linkedin_public_identifiers ([str]), funding_types ([str enum]),
    # funding_total_start/end, funding_event_date_featured_start/end,
    # job_post_* (nested job-post filters, same shape as search_job_posts),
    # simple_event_source/reason/date_featured_start/end.

    def search_organizations(self, **filters: Any) -> Dict[str, Any]:
        return self._request("POST", self._account_path("/datastorage/organization_search/"), json=filters)

    def search_organizations_totals(self, **filters: Any) -> Dict[str, Any]:
        return self._request("POST", self._account_path("/datastorage/organization_search/totals/"), json=filters)

    # ------------------------------------------------------------------
    # Websites

    def lookup_website(
        self, *, domain: str = None, organization_id: int = None,
        organization_linkedin_public_identifier: str = None,
    ) -> Dict[str, Any]:
        """One of domain / organization_id / organization_linkedin_public_identifier."""
        body = {}
        if domain is not None:
            body["domain"] = domain
        if organization_id is not None:
            body["organization_id"] = organization_id
        if organization_linkedin_public_identifier is not None:
            body["organization_linkedin_public_identifier"] = organization_linkedin_public_identifier
        return self._request("POST", self._account_path("/datastorage/website_detail_lookup/"), json=body)

    # ------------------------------------------------------------------
    # People — contact lookups, detail lookups, reverse lookups, role search.

    def lookup_person_personal_emails(
        self, *, person_id: int = None, linkedin_public_identifier: str = None,
    ) -> List[Dict[str, Any]]:
        """One of person_id / linkedin_public_identifier. Free-webmail addresses only."""
        body = {}
        if person_id is not None:
            body["person_id"] = person_id
        if linkedin_public_identifier is not None:
            body["linkedin_public_identifier"] = linkedin_public_identifier
        return self._request("POST", self._account_path("/datastorage/person_contacts_lookup/personal_emails/"), json=body)

    def lookup_person_phone_numbers(
        self, *, person_id: int = None, linkedin_public_identifier: str = None,
    ) -> List[Dict[str, Any]]:
        """One of person_id / linkedin_public_identifier."""
        body = {}
        if person_id is not None:
            body["person_id"] = person_id
        if linkedin_public_identifier is not None:
            body["linkedin_public_identifier"] = linkedin_public_identifier
        return self._request("POST", self._account_path("/datastorage/person_contacts_lookup/phone_numbers/"), json=body)

    def lookup_person_work_emails(
        self, *, person_id: int = None, linkedin_public_identifier: str = None,
        do_contacts_enrichment: bool = None,
    ) -> List[Dict[str, Any]]:
        """One of person_id / linkedin_public_identifier."""
        body = {}
        if person_id is not None:
            body["person_id"] = person_id
        if linkedin_public_identifier is not None:
            body["linkedin_public_identifier"] = linkedin_public_identifier
        if do_contacts_enrichment is not None:
            body["do_contacts_enrichment"] = do_contacts_enrichment
        return self._request("POST", self._account_path("/datastorage/person_contacts_lookup/work_emails/"), json=body)

    def lookup_person_detail(
        self, *, person_id: int = None, linkedin_public_identifier: str = None,
    ) -> Dict[str, Any]:
        """One of person_id / linkedin_public_identifier."""
        body = {}
        if person_id is not None:
            body["person_id"] = person_id
        if linkedin_public_identifier is not None:
            body["linkedin_public_identifier"] = linkedin_public_identifier
        return self._request("POST", self._account_path("/datastorage/person_detail_lookup/"), json=body)

    def lookup_person_by_email(self, email: str) -> Dict[str, Any]:
        """Raises `UpstreamHTTPError(404)` on no match (confirmed live) —
        unlike the contact-lookup methods above, which return `[]`."""
        return self._request("POST", self._account_path("/datastorage/person_detail_reverse_lookup/by_email/"), json={"email": email})

    def lookup_person_by_phone_number(self, phone_number: str) -> Dict[str, Any]:
        """Raises `UpstreamHTTPError(404)` on no match (confirmed live) —
        unlike the contact-lookup methods above, which return `[]`."""
        return self._request(
            "POST", self._account_path("/datastorage/person_detail_reverse_lookup/by_phone_number/"),
            json={"phone_number": phone_number},
        )

    # Filters (all optional, via **filters): page, role_title,
    # role_description, role_is_current, role_position_start_date/end_date,
    # role_years_on_position_start/end, person_name, person_headline,
    # person_description, person_skills/person_locations/person_industries/
    # person_industries_exclude ([int] IDs — via autocomplete),
    # person_linkedin_public_identifiers ([str]), organizations ([int]),
    # organizations_bulk_domain, organization_domains ([str]),
    # organization_description, organization_locations/industries/
    # industries_exclude/keywords/web_technologies ([int] IDs), plus the
    # same organization_founded_date_*/employees_*/revenue_*/domain_rank_*,
    # funding_*, job_post_*, simple_event_* filters as search_organizations.

    def search_person_roles(self, **filters: Any) -> Dict[str, Any]:
        return self._request("POST", self._account_path("/datastorage/person_role_search/"), json=filters)

    def search_person_roles_totals(self, **filters: Any) -> Dict[str, Any]:
        """Unlike other totals endpoints, also breaks down total_persons / total_organizations."""
        return self._request("POST", self._account_path("/datastorage/person_role_search/totals/"), json=filters)

    # ------------------------------------------------------------------
    # Subscription / credit usage — reads, no credit cost.

    def list_balance_change_logs(
        self, *, date_created_start: str = None, date_created_end: str = None, page: int = None,
    ) -> Dict[str, Any]:
        params = {}
        if date_created_start is not None:
            params["date_created_start"] = date_created_start
        if date_created_end is not None:
            params["date_created_end"] = date_created_end
        if page is not None:
            params["page"] = page
        return self._request("GET", self._account_path("/subscriptions/balance_change_logs/"), params=params)

    def get_balance_change_totals(
        self, *, date_created_start: str = None, date_created_end: str = None,
    ) -> Dict[str, Any]:
        params = {}
        if date_created_start is not None:
            params["date_created_start"] = date_created_start
        if date_created_end is not None:
            params["date_created_end"] = date_created_end
        return self._request("GET", self._account_path("/subscriptions/balance_change_logs/totals/"), params=params)

    # ------------------------------------------------------------------
    # Autocomplete — resolves free text to the integer IDs the search/filter
    # fields above require. `q` required, `page` optional. Free (confirmed
    # live). ⚠️ `results[].id` comes back as a STRING (e.g. "9839") —
    # confirmed live — cast to `int` before using it in a search filter.

    def _autocomplete(self, kind: str, q: str, *, page: int = None) -> Dict[str, Any]:
        params = {"q": q}
        if page is not None:
            params["page"] = page
        return self._request("GET", self._account_path(f"/datastorage/autocomplete/{kind}/"), params=params)

    def autocomplete_industries(self, q: str, *, page: int = None) -> Dict[str, Any]:
        return self._autocomplete("industries", q, page=page)

    def autocomplete_organizations(self, q: str, *, page: int = None) -> Dict[str, Any]:
        return self._autocomplete("organizations", q, page=page)

    def autocomplete_organization_keywords(self, q: str, *, page: int = None) -> Dict[str, Any]:
        return self._autocomplete("organization_keywords", q, page=page)

    def autocomplete_locations(self, q: str, *, page: int = None) -> Dict[str, Any]:
        return self._autocomplete("locations", q, page=page)

    def autocomplete_person_skills(self, q: str, *, page: int = None) -> Dict[str, Any]:
        return self._autocomplete("person_skills", q, page=page)

    def autocomplete_web_technologies(self, q: str, *, page: int = None) -> Dict[str, Any]:
        return self._autocomplete("web_technologies", q, page=page)
