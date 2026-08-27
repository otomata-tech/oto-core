"""Produits premium LinkedIn : contrats, InMail, pipeline, offres & candidats.

Extrait de `client.py` (découpage par domaine, surface publique figée) :
les corps sont inchangés. Ce mixin n'est jamais instancié seul — il est
composé dans `UnipileClient`, qui fournit le transport (`_request`,
`_acct`, `_norm`, `_by_shape`, `session`).
"""

from __future__ import annotations

from typing import Any, Optional
from urllib.parse import quote

from ..errors import UnipileError


class _PremiumMixin:
    """Produits premium LinkedIn : contrats, InMail, pipeline, offres & candidats."""

    def list_contracts(self) -> dict:
        return self._request("GET", self._acct("/linkedin/contracts"))

    def select_contract(self, contract_id: str) -> dict:
        return self._request(
            "POST",
            self._acct(f"/linkedin/contracts/{quote(contract_id, safe='')}/select"),
        )

    def inmail_balance(self) -> dict:
        """Solde InMail. v2 : `GET /linkedin/inmail-credits`. Réponse `{object, credits}`."""
        return self._request("GET", self._acct("/linkedin/inmail-credits"))

    def endorse_profile(self, profile_id: str, skill_endorsement_id: int) -> dict:
        """v2 : `POST /linkedin/member/{member_id}/endorse-skill`, corps
        `{skill_id}`."""
        return self._request(
            "POST",
            self._acct(f"/linkedin/member/{quote(profile_id, safe='')}/endorse-skill"),
            json={"skill_id": str(skill_endorsement_id)},
        )

    def member_action(self, user_id: str, api: str, action: str,
                     hiring_project_id: Optional[str] = None,
                     stage: Optional[str] = None,
                     list_id: Optional[str] = None) -> dict:
        """Action premium (sauvegarde lead / pipeline recruteur). v2 éclate ces
        actions par produit ; on mappe les cas courants, sinon erreur claire."""
        if api == "sales_navigator" and action == "saveLead":
            if not list_id:
                raise UnipileError("saveLead : list_id (lead-list) requis.")
            return self._request(
                "POST",
                self._acct(
                    f"/linkedin/sales-navigator/lead-lists/{quote(list_id, safe='')}/save"
                ),
                json={"user_id": user_id},
            )
        if api == "recruiter" and action in (
            "addCandidateToPipeline", "addApplicantToPipeline"
        ):
            if not hiring_project_id:
                raise UnipileError(
                    "pipeline recruiter : hiring_project_id requis."
                )
            body: dict[str, Any] = {"user_id": user_id}
            if stage:
                body["stage"] = stage
            return self._request(
                "POST",
                self._acct(
                    f"/linkedin/recruiter/projects/"
                    f"{quote(hiring_project_id, safe='')}/pipeline/candidate/save"
                ),
                json=body,
            )
        raise UnipileError(
            f"member_action : combinaison api={api!r} action={action!r} "
            "non mappée."
        )

    # ---- recruiter : offres & candidats ---------------------------------

    def list_job_postings(self, cursor: Optional[str] = None,
                         limit: Optional[int] = None) -> dict:
        params: dict[str, Any] = {}
        if cursor:
            params["cursor"] = cursor
        if limit:
            params["limit"] = limit
        return self._norm(self._request(
            "GET", self._acct("/linkedin/jobs"), params=params
        ))

    def get_job_posting(self, job_id: str) -> dict:
        return self._request(
            "GET", self._acct(f"/linkedin/jobs/{quote(job_id, safe='')}")
        )

    def list_job_applicants(self, job_id: str, cursor: Optional[str] = None,
                           limit: Optional[int] = None) -> dict:
        """v2 : `POST /linkedin/jobs/{job_id}/applicants` (getClassicApplicants)."""
        body: dict[str, Any] = {}
        if cursor:
            body["cursor"] = cursor
        if limit:
            body["limit"] = limit
        return self._norm(self._request(
            "POST", self._acct(f"/linkedin/jobs/{quote(job_id, safe='')}/applicants"),
            json=body,
        ))

    def get_job_applicant(self, job_id: str, applicant_id: str) -> dict:
        return self._request(
            "GET",
            self._acct(
                f"/linkedin/jobs/{quote(job_id, safe='')}"
                f"/applicants/{quote(applicant_id, safe='')}"
            ),
        )

    def list_hiring_projects(self, cursor: Optional[str] = None,
                            limit: Optional[int] = None) -> dict:
        params: dict[str, Any] = {}
        if cursor:
            params["cursor"] = cursor
        if limit:
            params["limit"] = limit
        return self._norm(self._request(
            "GET", self._acct("/linkedin/recruiter/projects"), params=params
        ))
