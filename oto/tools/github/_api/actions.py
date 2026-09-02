"""GitHub Actions — workflows, exécutions, jobs, logs, artefacts.

Ce mixin n'est jamais instancié seul : il est composé dans `GitHubClient`, qui
fournit le transport (`_request`, `_get`, `_check_choice`).

⚠️ **Toutes les listes d'Actions sont des OBJETS, pas des tableaux** :
`{total_count, workflow_runs: [...]}`, `{total_count, jobs: [...]}`,
`{total_count, artifacts: [...]}`. Une boucle écrite pour un tableau nu itérerait
sur les CLÉS du dict sans lever. `client.iterate()` connaît ces enveloppes ; ne
pas réécrire la pagination à la main ici.

⚠️ **Logs et artefacts se téléchargent par REDIRECTION** : GitHub répond 302 vers
une URL de stockage signée, à durée de vie courte (≈ 1 minute), et **cette URL ne
doit PAS recevoir l'en-tête `Authorization`** — le stockage la rejetterait. Les
méthodes de téléchargement d'ici rendent donc la réponse brute et exposent
l'URL, plutôt que de suivre la redirection avec les en-têtes de la session.

⚠️ **Relancer et annuler sont des actions RÉELLES sur l'infrastructure** :
`rerun_workflow_run` réexécute un pipeline (donc consomme des minutes facturées
et peut redéployer), `cancel_workflow_run` interrompt un travail en cours, et
`dispatch_workflow` déclenche un workflow — potentiellement un déploiement.

**Délibérément absent** : les secrets et variables Actions. Les poser par un
connecteur reviendrait à déplacer des credentials d'un coffre vers un autre, ce
que l'architecture d'oto refuse par principe.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from ..const import RUN_STATUSES


class _ActionsMixin:
    """Workflows, exécutions, jobs, artefacts."""

    # --- workflows ------------------------------------------------------------

    def list_workflows(self, owner: str, repo: str,
                       per_page: Optional[int] = None,
                       page: Optional[int] = None) -> Any:
        """GET /repos/{owner}/{repo}/actions/workflows — workflows définis.

        ⚠️ Rend `{total_count, workflows: [...]}`, pas un tableau.
        """
        return self._get(f"/repos/{owner}/{repo}/actions/workflows", None,
                         per_page, page)

    def get_workflow(self, owner: str, repo: str, workflow: Any) -> Any:
        """GET /repos/{owner}/{repo}/actions/workflows/{workflow} — un workflow.

        `workflow` accepte l'**id numérique OU le nom de fichier**
        (`ci.yml`) — le nom de fichier est plus stable dans le temps.
        """
        return self._request(
            "GET", f"/repos/{owner}/{repo}/actions/workflows/{workflow}")

    def dispatch_workflow(self, owner: str, repo: str, workflow: Any,
                          ref: str, inputs: Optional[Dict[str, Any]] = None) -> Any:
        """POST /…/actions/workflows/{workflow}/dispatches — **DÉCLENCHE un workflow**.

        ⚠️ Exécution réelle : peut construire, tester, publier ou **déployer**.
        `ref` est la branche ou le tag sur lequel tourner.

        ⚠️ **Le workflow doit déclarer `workflow_dispatch`** dans ses
        déclencheurs, sinon GitHub répond 404 — un 404 ici veut dire « pas
        déclenchable », pas « n'existe pas ».

        ⚠️ Réponse **204 sans corps** : elle ne rend PAS l'exécution créée. Pour
        la retrouver, lister les exécutions du workflow juste après (il y a un
        court délai avant qu'elle apparaisse).
        """
        if not ref:
            raise ValueError(
                "`ref` requis : la branche ou le tag sur lequel déclencher.")
        body: Dict[str, Any] = {"ref": ref}
        if inputs:
            body["inputs"] = inputs
        return self._request(
            "POST",
            f"/repos/{owner}/{repo}/actions/workflows/{workflow}/dispatches",
            json=body)

    # --- exécutions -------------------------------------------------------------

    def list_workflow_runs(self, owner: str, repo: str,
                           workflow: Optional[Any] = None,
                           actor: Optional[str] = None,
                           branch: Optional[str] = None,
                           event: Optional[str] = None,
                           status: Optional[str] = None,
                           created: Optional[str] = None,
                           head_sha: Optional[str] = None,
                           per_page: Optional[int] = None,
                           page: Optional[int] = None) -> Any:
        """GET /…/actions/runs — exécutions du dépôt (ou d'un workflow donné).

        Avec `workflow`, interroge `/actions/workflows/{workflow}/runs`.
        `created` accepte la syntaxe d'intervalle de GitHub (`>=2026-01-01`).

        ⚠️ Rend `{total_count, workflow_runs: [...]}`, pas un tableau.
        ⚠️ `status` mélange des états d'avancement (`queued`, `in_progress`,
        `completed`) et des CONCLUSIONS (`success`, `failure`, `cancelled`…) dans
        un même paramètre — c'est bien l'API qui est ainsi.
        """
        self._check_choice("status", status, RUN_STATUSES)
        path = (f"/repos/{owner}/{repo}/actions/workflows/{workflow}/runs"
                if workflow is not None
                else f"/repos/{owner}/{repo}/actions/runs")
        return self._get(path, {
            "actor": actor, "branch": branch, "event": event,
            "status": status, "created": created, "head_sha": head_sha},
            per_page, page)

    def get_workflow_run(self, owner: str, repo: str, run_id: Any) -> Any:
        """GET /repos/{owner}/{repo}/actions/runs/{run_id} — une exécution.

        ⚠️ Lire `status` ET `conclusion` : une exécution `completed` peut avoir
        échoué. `conclusion` est `null` tant que le travail n'est pas fini.
        """
        return self._request("GET",
                             f"/repos/{owner}/{repo}/actions/runs/{run_id}")

    def cancel_workflow_run(self, owner: str, repo: str, run_id: Any) -> Any:
        """POST /…/actions/runs/{run_id}/cancel — **ANNULE une exécution en cours**.

        ⚠️ Interrompt un travail réel. Réponse **202** : l'annulation est
        demandée, pas encore effective.
        """
        return self._request(
            "POST", f"/repos/{owner}/{repo}/actions/runs/{run_id}/cancel")

    def rerun_workflow_run(self, owner: str, repo: str, run_id: Any,
                           enable_debug_logging: Optional[bool] = None) -> Any:
        """POST /…/actions/runs/{run_id}/rerun — **RELANCE toute l'exécution**.

        ⚠️ Exécution réelle : consomme des minutes facturées, et peut redéployer
        si le workflow déploie. Pour ne rejouer que ce qui a échoué,
        `rerun_failed_jobs` — moins coûteux et moins risqué.
        """
        body = ({"enable_debug_logging": enable_debug_logging}
                if enable_debug_logging is not None else None)
        return self._request(
            "POST", f"/repos/{owner}/{repo}/actions/runs/{run_id}/rerun",
            json=body)

    def rerun_failed_jobs(self, owner: str, repo: str, run_id: Any,
                          enable_debug_logging: Optional[bool] = None) -> Any:
        """POST /…/actions/runs/{run_id}/rerun-failed-jobs — relance les jobs EN ÉCHEC.

        Les jobs réussis ne sont pas rejoués : c'est la relance la moins chère et
        la moins risquée des deux.
        """
        body = ({"enable_debug_logging": enable_debug_logging}
                if enable_debug_logging is not None else None)
        return self._request(
            "POST",
            f"/repos/{owner}/{repo}/actions/runs/{run_id}/rerun-failed-jobs",
            json=body)

    def delete_workflow_run(self, owner: str, repo: str, run_id: Any) -> Any:
        """DELETE /repos/{owner}/{repo}/actions/runs/{run_id} — supprime une exécution.

        ⚠️ Définitif : emporte ses logs et son historique. Une exécution en cours
        ne peut pas être supprimée (l'annuler d'abord).
        """
        return self._request(
            "DELETE", f"/repos/{owner}/{repo}/actions/runs/{run_id}")

    # --- jobs ---------------------------------------------------------------------

    def list_run_jobs(self, owner: str, repo: str, run_id: Any,
                      filter: Optional[str] = None,
                      per_page: Optional[int] = None,
                      page: Optional[int] = None) -> Any:
        """GET /…/actions/runs/{run_id}/jobs — jobs d'une exécution.

        `filter="all"` inclut les tentatives précédentes (défaut : `latest`).
        ⚠️ Rend `{total_count, jobs: [...]}`, pas un tableau.
        """
        return self._get(f"/repos/{owner}/{repo}/actions/runs/{run_id}/jobs",
                         {"filter": filter}, per_page, page)

    def get_job(self, owner: str, repo: str, job_id: Any) -> Any:
        """GET /repos/{owner}/{repo}/actions/jobs/{job_id} — un job et ses étapes."""
        return self._request("GET",
                             f"/repos/{owner}/{repo}/actions/jobs/{job_id}")

    def get_job_logs_url(self, owner: str, repo: str, job_id: Any) -> Optional[str]:
        """GET /…/actions/jobs/{job_id}/logs — l'URL SIGNÉE des logs d'un job.

        ⚠️ GitHub répond **302** vers une URL de stockage à durée de vie courte
        (≈ 1 minute). Cette méthode ne suit PAS la redirection et rend l'URL :
        la rejouer avec l'en-tête `Authorization` de la session ferait échouer le
        stockage, qui refuse une requête doublement authentifiée. Télécharger
        l'URL rendue **sans en-tête d'auth**, tout de suite.

        Rend `None` si GitHub n'a pas redirigé (logs expirés ou absents — ils
        sont conservés un temps limité).
        """
        resp = self._request(
            "GET", f"/repos/{owner}/{repo}/actions/jobs/{job_id}/logs",
            raw=True)
        return (getattr(resp, "headers", None) or {}).get("Location")

    # --- artefacts -----------------------------------------------------------------

    def list_artifacts(self, owner: str, repo: str,
                       run_id: Optional[Any] = None,
                       name: Optional[str] = None,
                       per_page: Optional[int] = None,
                       page: Optional[int] = None) -> Any:
        """GET /…/actions/artifacts — artefacts du dépôt (ou d'une exécution).

        Avec `run_id`, interroge `/actions/runs/{run_id}/artifacts`.
        ⚠️ Rend `{total_count, artifacts: [...]}`, pas un tableau.
        ⚠️ Un artefact **expire** (90 jours par défaut) : `expired: true` signale
        une entrée dont le contenu n'est plus téléchargeable.
        """
        path = (f"/repos/{owner}/{repo}/actions/runs/{run_id}/artifacts"
                if run_id is not None
                else f"/repos/{owner}/{repo}/actions/artifacts")
        return self._get(path, {"name": name}, per_page, page)

    def get_artifact(self, owner: str, repo: str, artifact_id: Any) -> Any:
        """GET /repos/{owner}/{repo}/actions/artifacts/{id} — un artefact."""
        return self._request(
            "GET", f"/repos/{owner}/{repo}/actions/artifacts/{artifact_id}")

    def get_artifact_download_url(self, owner: str, repo: str,
                                  artifact_id: Any,
                                  archive_format: str = "zip") -> Optional[str]:
        """GET /…/artifacts/{id}/{format} — l'URL SIGNÉE de téléchargement.

        Même règle que les logs : **302** vers une URL de stockage éphémère, à
        télécharger **sans en-tête `Authorization`**. Rend `None` si GitHub n'a
        pas redirigé (artefact expiré).
        """
        resp = self._request(
            "GET",
            f"/repos/{owner}/{repo}/actions/artifacts/{artifact_id}/{archive_format}",
            raw=True)
        return (getattr(resp, "headers", None) or {}).get("Location")

    def delete_artifact(self, owner: str, repo: str, artifact_id: Any) -> Any:
        """DELETE /repos/{owner}/{repo}/actions/artifacts/{id} — supprime un artefact."""
        return self._request(
            "DELETE", f"/repos/{owner}/{repo}/actions/artifacts/{artifact_id}")
