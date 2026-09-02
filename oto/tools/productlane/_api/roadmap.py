"""Roadmap Productlane — projets et issues, tous deux **adossés à Linear**.

Ce mixin n'est jamais instancié seul : il est composé dans `ProductlaneClient`,
qui fournit le transport (`_request`, `_list`, `_check_choice`).

⚠️ **Ce n'est pas une roadmap autonome : Linear doit être connecté.** Trois
conséquences qu'il vaut mieux connaître avant de lire un retour d'appel :

- **la création part de Linear** — une issue est déposée LÀ-BAS d'abord, puis
  reflétée ici ; sans Linear connecté, la création échoue ;
- **la mise à jour, non** — une écriture locale réussit même si la synchro Linear
  échoue : l'échec est journalisé côté éditeur et **ne remonte pas dans la
  réponse**. Un `200` ne prouve donc pas que Linear a suivi ;
- **la suppression archive** dans Linear et soft-delete ici, avec la même
  asymétrie.

`team_id`, `state_id`, `assignee_id`, `label_ids`, `linear_status_id` sont des
identifiants **Linear**, pas Productlane : les lire via `list_workflow_states`,
`list_project_statuses` et le connecteur Linear.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from ..const import PROJECT_STATES, ROADMAP_SORTS


class _RoadmapMixin:
    """Projets et issues de la roadmap."""

    # --- projets ------------------------------------------------------------

    def list_projects(self, limit: Optional[int] = None,
                      cursor: Optional[str] = None,
                      state: Optional[str] = None,
                      name_contains: Optional[str] = None,
                      linear_team_id: Optional[str] = None,
                      sort: Optional[str] = None,
                      created_after: Optional[str] = None,
                      created_before: Optional[str] = None,
                      updated_after: Optional[str] = None,
                      updated_before: Optional[str] = None) -> Any:
        """GET /projects — projets de la roadmap. Scope `projects:read`.

        `sort="total_score"` classe par poids des retours clients rattachés
        (customer needs), là où `created_at` est l'ordre par défaut des listes v2.
        """
        self._check_choice("state", state, PROJECT_STATES)
        self._check_choice("sort", sort, ROADMAP_SORTS)
        return self._list("/projects", limit, cursor, {
            "state": state, "name_contains": name_contains,
            "linear_team_id": linear_team_id, "sort": sort,
            "created_after": created_after, "created_before": created_before,
            "updated_after": updated_after, "updated_before": updated_before,
        })

    def get_project(self, project_id: str) -> Any:
        """GET /projects/{id} — un projet. Scope `projects:read`."""
        return self._request("GET", f"/projects/{project_id}")

    def list_project_statuses(self) -> Any:
        """GET /projects/statuses — statuts de projet Linear, au niveau organisation.

        Scope `projects:read`, **Linear connecté requis**. Sert à remplir
        `linear_status_id`.
        """
        return self._request("GET", "/projects/statuses")

    def create_project(self, payload: Dict[str, Any]) -> Any:
        """POST /projects — crée un projet, **synchronisé vers Linear**.

        Scope `projects:write`, Linear connecté requis. Requis : `name`,
        `team_id` (identifiant d'équipe LINEAR). Optionnels : `description`,
        `icon`, `color`, `state`, `linear_status_id`, `is_visible`.

        `is_visible` décide de la présence sur la **roadmap publique**.
        """
        self._check_choice("state", payload.get("state"), PROJECT_STATES)
        return self._request("POST", "/projects", json=dict(payload))

    def update_project(self, project_id: str, payload: Dict[str, Any]) -> Any:
        """PATCH /projects/{id} — met à jour un projet. Scope `projects:write`.

        Champs : `name`, `description`, `icon`, `color`, `state`,
        `linear_status_id`, `is_visible`.

        ⚠️ Un échec de synchro Linear est journalisé côté éditeur et **ne bloque
        pas** la mise à jour locale : la réponse peut être un succès alors que
        Linear n'a pas suivi.
        """
        self._check_choice("state", payload.get("state"), PROJECT_STATES)
        return self._request("PATCH", f"/projects/{project_id}",
                             json=dict(payload))

    def delete_project(self, project_id: str) -> Any:
        """DELETE /projects/{id} — archive dans Linear, soft-delete ici.

        Scope `projects:write`. Un échec côté Linear ne bloque pas le
        soft-delete local.
        """
        return self._request("DELETE", f"/projects/{project_id}")

    # --- issues -------------------------------------------------------------

    def list_issues(self, limit: Optional[int] = None,
                    cursor: Optional[str] = None,
                    project_id: Optional[str] = None,
                    status: Optional[str] = None,
                    name_contains: Optional[str] = None,
                    linear_team_id: Optional[str] = None,
                    sort: Optional[str] = None,
                    created_after: Optional[str] = None,
                    created_before: Optional[str] = None,
                    updated_after: Optional[str] = None,
                    updated_before: Optional[str] = None) -> Any:
        """GET /issues — issues de la roadmap. Scope `issues:read`.

        ⚠️ `status` n'est PAS une énumération fermée ici : les états d'issue sont
        les **workflow states de l'équipe Linear**, propres à chaque workspace.
        Les lire via `list_workflow_states(team_id)` — coder une valeur en dur
        marcherait chez un client et pas chez le suivant.
        """
        self._check_choice("sort", sort, ROADMAP_SORTS)
        return self._list("/issues", limit, cursor, {
            "project_id": project_id, "status": status,
            "name_contains": name_contains, "linear_team_id": linear_team_id,
            "sort": sort,
            "created_after": created_after, "created_before": created_before,
            "updated_after": updated_after, "updated_before": updated_before,
        })

    def get_issue(self, issue_id: str) -> Any:
        """GET /issues/{id} — une issue. Scope `issues:read`."""
        return self._request("GET", f"/issues/{issue_id}")

    def list_workflow_states(self, team_id: str) -> Any:
        """GET /issues/workflow-states — états Linear d'une équipe. Scope `issues:read`.

        `team_id` est **requis** par l'amont, et Linear doit être connecté. C'est
        la source des `state_id` à passer à `create_issue` / `update_issue`.
        """
        if not team_id:
            raise ValueError(
                "`team_id` est requis : les états de workflow sont propres à une "
                "équipe Linear.")
        return self._request("GET", "/issues/workflow-states",
                             params={"team_id": team_id})

    def create_issue(self, payload: Dict[str, Any]) -> Any:
        """POST /issues — crée une issue, **déposée dans Linear d'abord**.

        Scope `issues:write`, Linear connecté requis. Requis : `title`,
        `team_id`, `state_id`, `priority`. Optionnels : `description`,
        `project_id`, `assignee_id`, `label_ids`, `is_visible`.

        ⚠️ `priority` suit la numérotation **Linear** : `0` = aucune priorité,
        `1` = urgente, puis 2, 3, 4 par ordre décroissant d'urgence. Ce n'est pas
        une échelle croissante, et `0` ne veut pas dire « la plus basse ».
        """
        return self._request("POST", "/issues", json=dict(payload))

    def update_issue(self, issue_id: str, payload: Dict[str, Any]) -> Any:
        """PATCH /issues/{id} — met à jour une issue. Scope `issues:write`.

        Champs : `title`, `description`, `state_id`, `priority`, `project_id`,
        `assignee_id`, `is_visible`. Même asymétrie que les projets : un échec de
        synchro Linear **ne bloque pas** l'écriture locale.
        """
        return self._request("PATCH", f"/issues/{issue_id}", json=dict(payload))

    def delete_issue(self, issue_id: str) -> Any:
        """DELETE /issues/{id} — archive dans Linear, soft-delete ici.

        Scope `issues:write`.
        """
        return self._request("DELETE", f"/issues/{issue_id}")
