"""Contrat du client GitHub (REST v3, Bearer, pagination `Link`).

Mocke `requests.Session.request` : en-têtes, verbes et chemins, bornes de
pagination, énumérations, re-tentatives. Quatre pièges de cette API sont
verrouillés ici en priorité, parce qu'aucun ne se manifeste par une erreur :

1. `per_page > 100` est **raboté en silence** par GitHub — refusé localement ;
2. **une pull request EST une issue**, donc `/issues` en rend aussi ;
3. plusieurs listes sont des **objets** (`items`, `workflow_runs`, `jobs`…) et
   non des tableaux — une boucle naïve itérerait sur les clés ;
4. certains endpoints répondent **204/404 sans corps** pour dire oui/non : le
   404 y est une réponse, pas une erreur.
"""
from __future__ import annotations

import base64

import pytest

from oto.tools.common.errors import UpstreamHTTPError
from oto.tools.github import client as gh


class _Resp:
    def __init__(self, status_code: int = 200, body=None, headers=None,
                 content: bytes = b"x"):
        self.status_code = status_code
        self._body = body if body is not None else []
        self.content = content
        self.text = str(self._body)
        self.headers = headers or {}

    def json(self):
        if self._body is None:
            raise ValueError("pas du JSON")
        return self._body


@pytest.fixture()
def capture(monkeypatch):
    seen = {"calls": []}

    def fake_request(self, method, url, **kwargs):
        seen.update(method=method, url=url, kwargs=kwargs)
        seen["calls"].append((method, url))
        return _Resp(200)

    monkeypatch.setattr(gh.requests.Session, "request", fake_request)
    monkeypatch.setattr(gh.time, "sleep", lambda _s: None)
    return seen


@pytest.fixture()
def cli():
    return gh.GitHubClient(token="ghp_secret")


# --- en-têtes et authentification -------------------------------------------

def test_entetes_obligatoires(cli, capture):
    cli.get_repo("octo", "hello")
    h = cli.session.headers
    assert h["Authorization"] == "Bearer ghp_secret"
    assert h["Accept"] == "application/vnd.github+json"
    assert h["X-GitHub-Api-Version"] == gh.DEFAULT_API_VERSION


def test_le_jeton_ne_part_jamais_en_query(cli, capture):
    cli.list_issues("octo", "hello")
    params = capture["kwargs"].get("params") or []
    assert "ghp_secret" not in capture["url"]
    assert all("ghp_secret" not in str(v) for _k, v in params)


def test_base_url_personnalisee_pour_enterprise_server():
    c = gh.GitHubClient(token="t", base_url="https://ghe.interne/api/v3/")
    assert c.base_url == "https://ghe.interne/api/v3"


def test_me_est_la_sonde(cli, capture):
    cli.me()
    assert capture["url"] == "https://api.github.com/user"


# --- piège nº 1 : per_page raboté en silence --------------------------------

@pytest.mark.parametrize("bad", [0, 101, 500, -1])
def test_per_page_hors_bornes_refuse_localement(cli, bad):
    """GitHub ne renverrait PAS d'erreur : il rabote à 100. Le refus local est
    la seule façon de distinguer « j'ai tout » de « j'ai les cent premiers »."""
    with pytest.raises(ValueError, match="entre 1 et 100"):
        cli.list_issues("octo", "hello", per_page=bad)


def test_le_message_dit_que_github_raboterait_en_silence(cli):
    with pytest.raises(ValueError, match="SANS erreur"):
        cli.list_issues("octo", "hello", per_page=250)


@pytest.mark.parametrize("ok", [1, 30, 100])
def test_per_page_dans_les_bornes_passe(cli, capture, ok):
    cli.list_issues("octo", "hello", per_page=ok)
    assert ("per_page", ok) in capture["kwargs"]["params"]


def test_per_page_booleen_refuse(cli):
    with pytest.raises(ValueError, match="doit être un entier"):
        cli.list_issues("octo", "hello", per_page=True)


# --- piège nº 2 : une PR est une issue --------------------------------------

def test_les_pull_requests_sont_ecartees_des_issues_par_defaut(cli, monkeypatch):
    lignes = [
        {"number": 1, "title": "vraie issue"},
        {"number": 2, "title": "une PR", "pull_request": {"url": "..."}},
    ]
    monkeypatch.setattr(gh.requests.Session, "request",
                        lambda self, m, u, **k: _Resp(200, lignes))
    issues = cli.list_issues("octo", "hello")
    assert [i["number"] for i in issues] == [1]


def test_on_peut_demander_les_pull_requests_avec_les_issues(cli, monkeypatch):
    lignes = [{"number": 1}, {"number": 2, "pull_request": {}}]
    monkeypatch.setattr(gh.requests.Session, "request",
                        lambda self, m, u, **k: _Resp(200, lignes))
    assert len(cli.list_issues("octo", "hello",
                               include_pull_requests=True)) == 2


# --- piège nº 3 : les listes ne sont pas toutes des tableaux -----------------

@pytest.mark.parametrize("payload,attendu", [
    ([{"id": 1}], [{"id": 1}]),
    ({"total_count": 1, "items": [{"id": 2}]}, [{"id": 2}]),
    ({"total_count": 1, "workflow_runs": [{"id": 3}]}, [{"id": 3}]),
    ({"total_count": 1, "jobs": [{"id": 4}]}, [{"id": 4}]),
    ({"total_count": 1, "artifacts": [{"id": 5}]}, [{"id": 5}]),
    ({"total_count": 0, "items": []}, []),
    ({"rien": "ici"}, []),
    ("pas une liste", []),
])
def test_les_lignes_sont_extraites_de_toute_enveloppe(cli, payload, attendu):
    assert cli._rows(payload) == attendu


def test_rows_nitere_jamais_sur_les_cles_dun_dict(cli):
    """Le vrai danger : sans normalisation, une boucle rendrait les CLÉS
    (« total_count », « items ») en croyant rendre des lignes."""
    assert cli._rows({"total_count": 3, "items": [{"id": 1}]}) == [{"id": 1}]


# --- pagination par en-tête Link --------------------------------------------

def test_iterate_suit_le_lien_next_puis_sarrete(cli, monkeypatch):
    pages = [
        (_Resp(200, [{"id": 1}],
               {"Link": '<https://api.github.com/x?page=2>; rel="next"'})),
        (_Resp(200, [{"id": 2}], {})),          # plus de Link = fin
    ]
    etat = {"i": 0}

    def fake(self, method, url, **kwargs):
        r = pages[etat["i"]]
        etat["i"] += 1
        return r

    monkeypatch.setattr(gh.requests.Session, "request", fake)
    rows = list(cli.iterate(cli.list_issues, "octo", "hello"))
    assert [r["id"] for r in rows] == [1, 2]
    assert etat["i"] == 2


def test_iterate_sarrete_sur_page_vide(cli, monkeypatch):
    monkeypatch.setattr(
        gh.requests.Session, "request",
        lambda self, m, u, **k: _Resp(
            200, [], {"Link": '<https://x?page=2>; rel="next"'}))
    assert list(cli.iterate(cli.list_issues, "octo", "hello")) == []


def test_iterate_respecte_max_pages(cli, monkeypatch):
    monkeypatch.setattr(
        gh.requests.Session, "request",
        lambda self, m, u, **k: _Resp(
            200, [{"id": 1}], {"Link": '<https://x?page=9>; rel="next"'}))
    assert len(list(cli.iterate(cli.list_issues, "octo", "hello",
                                max_pages=4))) == 4


def test_iterate_refuse_une_page_fournie(cli):
    with pytest.raises(ValueError, match="gère la pagination"):
        list(cli.iterate(cli.list_issues, "octo", "hello", page=2))


def test_un_link_sans_next_ne_prolonge_pas(cli, monkeypatch):
    """`rel="prev"` seul (dernière page) ne doit pas être lu comme un `next`."""
    monkeypatch.setattr(
        gh.requests.Session, "request",
        lambda self, m, u, **k: _Resp(
            200, [{"id": 1}], {"Link": '<https://x?page=1>; rel="prev"'}))
    assert len(list(cli.iterate(cli.list_issues, "octo", "hello"))) == 1


# --- piège nº 4 : 204/404 comme réponse, pas comme erreur -------------------

@pytest.mark.parametrize("methode,args", [
    ("check_pull_merged", ("octo", "hello", 7)),
    ("check_org_membership", ("acme", "alice")),
    ("check_collaborator", ("octo", "hello", "alice")),
])
def test_204_vaut_oui_et_404_vaut_non(cli, monkeypatch, methode, args):
    for status, attendu in ((204, True), (404, False)):
        monkeypatch.setattr(gh.requests.Session, "request",
                            lambda self, m, u, _s=status, **k: _Resp(_s))
        assert getattr(cli, methode)(*args) is attendu


def test_un_vrai_refus_leve_quand_meme(cli, monkeypatch):
    """500 n'est ni un oui ni un non : il doit lever, pas rendre False."""
    monkeypatch.setattr(gh.requests.Session, "request",
                        lambda self, m, u, **k: _Resp(500))
    with pytest.raises(UpstreamHTTPError):
        cli.check_collaborator("octo", "hello", "alice")


# --- contenus de fichier -----------------------------------------------------

def test_lecture_de_fichier_decode_le_base64(cli, monkeypatch):
    contenu = base64.b64encode("bonjour".encode()).decode()
    monkeypatch.setattr(
        gh.requests.Session, "request",
        lambda self, m, u, **k: _Resp(200, {"type": "file", "size": 7,
                                            "encoding": "base64",
                                            "content": contenu}))
    assert cli.read_text_file("octo", "hello", "README.md") == "bonjour"


def test_lire_un_dossier_comme_un_fichier_est_nomme(cli, monkeypatch):
    monkeypatch.setattr(gh.requests.Session, "request",
                        lambda self, m, u, **k: _Resp(200, [{"name": "a"}]))
    with pytest.raises(ValueError, match="DOSSIER"):
        cli.read_text_file("octo", "hello", "src")


def test_un_fichier_servi_sans_contenu_est_nomme(cli, monkeypatch):
    """Au-delà de 1 Mo, GitHub omet `content` : sans ce refus, l'appelant
    recevrait une chaîne vide et croirait le fichier vide."""
    monkeypatch.setattr(
        gh.requests.Session, "request",
        lambda self, m, u, **k: _Resp(200, {"type": "file", "size": 2_000_000,
                                            "encoding": "none"}))
    with pytest.raises(ValueError, match="SANS contenu"):
        cli.read_text_file("octo", "hello", "gros.bin")


def test_un_binaire_est_nomme_comme_tel(cli, monkeypatch):
    contenu = base64.b64encode(b"\xff\xfe\x00\x01").decode()
    monkeypatch.setattr(
        gh.requests.Session, "request",
        lambda self, m, u, **k: _Resp(200, {"encoding": "base64",
                                            "content": contenu, "size": 4}))
    with pytest.raises(ValueError, match="binaire"):
        cli.read_text_file("octo", "hello", "img.png")


def test_ecriture_encode_le_contenu_en_base64(cli, capture):
    cli.create_or_update_file("octo", "hello", "a.txt", "msg", "coucou")
    envoye = capture["kwargs"]["json"]
    assert base64.b64decode(envoye["content"]).decode() == "coucou"
    assert capture["method"] == "PUT"


def test_ecriture_accepte_des_octets(cli, capture):
    cli.create_or_update_file("octo", "hello", "a.bin", "msg", b"\x00\x01")
    assert base64.b64decode(capture["kwargs"]["json"]["content"]) == b"\x00\x01"


def test_suppression_de_fichier_exige_le_sha(cli):
    with pytest.raises(ValueError, match="sha"):
        cli.delete_file("octo", "hello", "a.txt", "msg", "")


# --- garde-fous d'écriture ---------------------------------------------------

def test_etiquette_refuse_le_croisillon(cli):
    with pytest.raises(ValueError, match="sans `#`"):
        cli.create_label("octo", "hello", "bug", "#d73a4a")


def test_ouvrir_une_pr_exige_head_et_base(cli):
    with pytest.raises(ValueError, match="head"):
        cli.create_pull("octo", "hello", {"title": "t", "base": "main"})
    with pytest.raises(ValueError, match="base"):
        cli.create_pull("octo", "hello", {"title": "t", "head": "f"})


def test_ouvrir_une_pr_exige_un_titre_ou_une_issue(cli, capture):
    with pytest.raises(ValueError, match="title"):
        cli.create_pull("octo", "hello", {"head": "f", "base": "main"})
    cli.create_pull("octo", "hello", {"issue": 12, "head": "f", "base": "main"})
    assert capture["method"] == "POST"


def test_methode_de_fusion_inconnue_refusee(cli):
    with pytest.raises(ValueError, match="Valeurs acceptées"):
        cli.merge_pull("octo", "hello", 7, merge_method="fast-forward")


@pytest.mark.parametrize("m", gh.MERGE_METHODS)
def test_les_trois_methodes_de_fusion_passent(cli, capture, m):
    cli.merge_pull("octo", "hello", 7, merge_method=m)
    assert capture["kwargs"]["json"]["merge_method"] == m
    assert capture["url"].endswith("/pulls/7/merge")


def test_une_revue_sans_event_reste_en_attente(cli, capture):
    """`event` absent = revue PENDING, non publiée. C'est légal, et c'est le
    piège : le client ne doit pas en inventer un."""
    cli.create_review("octo", "hello", 7, {"body": "coucou"})
    assert "event" not in capture["kwargs"]["json"]


def test_event_de_revue_inconnu_refuse(cli):
    with pytest.raises(ValueError, match="Valeurs acceptées"):
        cli.create_review("octo", "hello", 7, {"event": "LGTM"})


def test_commentaire_de_revue_exige_sa_position(cli):
    with pytest.raises(ValueError, match="commit_id"):
        cli.create_review_comment("octo", "hello", 7, {"body": "x"})


def test_commentaire_en_reponse_nexige_pas_de_position(cli, capture):
    cli.create_review_comment("octo", "hello", 7,
                              {"body": "x", "in_reply_to": 42})
    assert capture["method"] == "POST"


def test_demander_une_revue_exige_une_cible(cli):
    with pytest.raises(ValueError, match="reviewers"):
        cli.request_reviewers("octo", "hello", 7)


def test_declencher_un_workflow_exige_une_ref(cli):
    with pytest.raises(ValueError, match="ref"):
        cli.dispatch_workflow("octo", "hello", "ci.yml", "")


def test_ajouter_des_etiquettes_exige_une_liste_non_vide(cli):
    with pytest.raises(ValueError, match="labels"):
        cli.add_labels("octo", "hello", 7, [])


def test_remplacer_les_etiquettes_accepte_une_liste_vide(cli, capture):
    """`set_labels([])` les retire toutes — c'est une intention, pas un oubli."""
    cli.set_labels("octo", "hello", 7, [])
    assert capture["method"] == "PUT"
    assert capture["kwargs"]["json"] == {"labels": []}


def test_depots_perso_type_exclusif_de_visibility(cli):
    with pytest.raises(ValueError, match="exclusif"):
        cli.list_my_repos(type="owner", visibility="private")


# --- recherche ---------------------------------------------------------------

@pytest.mark.parametrize("vide", ["", "   ", None])
def test_recherche_sans_terme_refusee(cli, vide):
    with pytest.raises(ValueError, match="`q` requis"):
        cli.search_code(vide)


def test_tri_de_recherche_hors_enum_refuse(cli):
    with pytest.raises(ValueError, match="Valeurs acceptées"):
        cli.search_repositories("oto", sort="downloads")


@pytest.mark.parametrize("payload,attendu", [
    ({"total_count": 12, "items": []}, False),
    ({"total_count": 12, "incomplete_results": True}, True),
    ({"total_count": 5000, "items": []}, True),      # au-delà du plafond 1000
    ({"total_count": 1000, "items": []}, False),
    ("pas un dict", False),
])
def test_troncature_de_recherche_detectee(cli, payload, attendu):
    assert cli.search_is_truncated(payload) is attendu


# --- re-tentatives et limites d'usage ---------------------------------------

def test_le_429_est_retente_en_lecture(cli, monkeypatch):
    n = {"i": 0}

    def flaky(self, method, url, **kwargs):
        n["i"] += 1
        return _Resp(429, headers={"Retry-After": "0"}) if n["i"] < 3 else _Resp(200)

    monkeypatch.setattr(gh.requests.Session, "request", flaky)
    monkeypatch.setattr(gh.time, "sleep", lambda _s: None)
    cli.list_issues("octo", "hello")
    assert n["i"] == 3


def test_un_403_de_permission_nest_PAS_retente(cli, monkeypatch):
    """GitHub sert 403 pour « pas le droit » (définitif) ET pour la limite
    secondaire (passager). Marteler une permission manquante est inutile."""
    n = {"i": 0}

    def refus(self, method, url, **kwargs):
        n["i"] += 1
        return _Resp(403, body={"message": "Resource not accessible"})

    monkeypatch.setattr(gh.requests.Session, "request", refus)
    with pytest.raises(UpstreamHTTPError) as exc:
        cli.list_issues("octo", "hello")
    assert exc.value.status_code == 403 and n["i"] == 1


def test_un_403_de_limite_secondaire_est_retente(cli, monkeypatch):
    n = {"i": 0}

    def flaky(self, method, url, **kwargs):
        n["i"] += 1
        if n["i"] == 1:
            return _Resp(403, headers={"Retry-After": "0"})
        return _Resp(200)

    monkeypatch.setattr(gh.requests.Session, "request", flaky)
    monkeypatch.setattr(gh.time, "sleep", lambda _s: None)
    cli.list_issues("octo", "hello")
    assert n["i"] == 2


def test_un_403_avec_quota_primaire_epuise_est_retente(cli, monkeypatch):
    n = {"i": 0}

    def flaky(self, method, url, **kwargs):
        n["i"] += 1
        if n["i"] == 1:
            return _Resp(403, headers={"x-ratelimit-remaining": "0",
                                       "x-ratelimit-reset": "0"})
        return _Resp(200)

    monkeypatch.setattr(gh.requests.Session, "request", flaky)
    monkeypatch.setattr(gh.time, "sleep", lambda _s: None)
    cli.list_issues("octo", "hello")
    assert n["i"] == 2


def test_une_ecriture_nest_jamais_retentee(cli, monkeypatch):
    """Aucune clé d'idempotence côté GitHub : rejouer un POST créerait une
    seconde issue, un second commentaire, un second commit."""
    n = {"i": 0}

    def always(self, method, url, **kwargs):
        n["i"] += 1
        return _Resp(429, headers={"Retry-After": "0"})

    monkeypatch.setattr(gh.requests.Session, "request", always)
    monkeypatch.setattr(gh.time, "sleep", lambda _s: None)
    with pytest.raises(UpstreamHTTPError):
        cli.create_issue("octo", "hello", {"title": "t"})
    assert n["i"] == 1


def test_lattente_du_quota_primaire_est_bornee(cli):
    """Un `reset` lointain (ou une horloge décalée) ne doit pas geler l'appelant
    une heure : l'attente est plafonnée."""
    resp = _Resp(403, headers={"x-ratelimit-remaining": "0",
                               "x-ratelimit-reset": "99999999999"})
    assert cli._retry_after(resp, 0) <= 60.0


def test_erreur_amont_porte_le_nom_du_service(cli, monkeypatch):
    monkeypatch.setattr(gh.requests.Session, "request",
                        lambda self, m, u, **k: _Resp(422, body={"message": "x"}))
    with pytest.raises(UpstreamHTTPError) as exc:
        cli.create_issue("octo", "hello", {"title": "t"})
    assert exc.value.status_code == 422 and exc.value.service == "github"


# --- téléchargements par redirection ----------------------------------------

def test_url_de_logs_lue_dans_la_redirection(cli, monkeypatch):
    monkeypatch.setattr(
        gh.requests.Session, "request",
        lambda self, m, u, **k: _Resp(302,
                                      headers={"Location": "https://blob/x"}))
    assert cli.get_job_logs_url("octo", "hello", 9) == "https://blob/x"


def test_absence_de_redirection_rend_none(cli, monkeypatch):
    """Logs expirés : pas de `Location`. Rendre None plutôt qu'une URL fausse."""
    monkeypatch.setattr(gh.requests.Session, "request",
                        lambda self, m, u, **k: _Resp(200, headers={}))
    assert cli.get_job_logs_url("octo", "hello", 9) is None


def test_le_telechargement_ne_suit_pas_la_redirection(cli, capture):
    """`allow_redirects=False` : l'URL signée refuserait l'en-tête Authorization
    de la session, donc on ne la suit surtout pas avec."""
    cli.get_artifact_download_url("octo", "hello", 3)
    assert capture["kwargs"]["allow_redirects"] is False


# --- verbes et chemins ------------------------------------------------------

@pytest.mark.parametrize("appel,attendu", [
    (lambda c: c.get_repo("o", "r"), ("GET", "/repos/o/r")),
    (lambda c: c.list_branches("o", "r"), ("GET", "/repos/o/r/branches")),
    (lambda c: c.get_commit("o", "r", "abc"), ("GET", "/repos/o/r/commits/abc")),
    (lambda c: c.compare_commits("o", "r", "a", "b"),
     ("GET", "/repos/o/r/compare/a...b")),
    (lambda c: c.get_content("o", "r", "a/b.py"),
     ("GET", "/repos/o/r/contents/a/b.py")),
    (lambda c: c.get_latest_release("o", "r"), ("GET", "/repos/o/r/releases/latest")),
    (lambda c: c.get_issue("o", "r", 3), ("GET", "/repos/o/r/issues/3")),
    (lambda c: c.create_issue_comment("o", "r", 3, "hi"),
     ("POST", "/repos/o/r/issues/3/comments")),
    (lambda c: c.update_issue_comment("o", "r", 88, "hi"),
     ("PATCH", "/repos/o/r/issues/comments/88")),
    (lambda c: c.remove_label("o", "r", 3, "bug"),
     ("DELETE", "/repos/o/r/issues/3/labels/bug")),
    (lambda c: c.list_pulls("o", "r"), ("GET", "/repos/o/r/pulls")),
    (lambda c: c.list_pull_files("o", "r", 3), ("GET", "/repos/o/r/pulls/3/files")),
    (lambda c: c.list_reviews("o", "r", 3), ("GET", "/repos/o/r/pulls/3/reviews")),
    (lambda c: c.submit_review("o", "r", 3, 9, "APPROVE"),
     ("POST", "/repos/o/r/pulls/3/reviews/9/events")),
    (lambda c: c.update_pull_branch("o", "r", 3),
     ("PUT", "/repos/o/r/pulls/3/update-branch")),
    (lambda c: c.get_org("acme"), ("GET", "/orgs/acme")),
    (lambda c: c.list_org_members("acme"), ("GET", "/orgs/acme/members")),
    (lambda c: c.set_org_membership("acme", "alice"),
     ("PUT", "/orgs/acme/memberships/alice")),
    (lambda c: c.remove_org_member("acme", "alice"),
     ("DELETE", "/orgs/acme/members/alice")),
    (lambda c: c.list_teams("acme"), ("GET", "/orgs/acme/teams")),
    (lambda c: c.add_team_member("acme", "core", "alice"),
     ("PUT", "/orgs/acme/teams/core/memberships/alice")),
    (lambda c: c.get_collaborator_permission("o", "r", "alice"),
     ("GET", "/repos/o/r/collaborators/alice/permission")),
    (lambda c: c.list_workflows("o", "r"), ("GET", "/repos/o/r/actions/workflows")),
    (lambda c: c.dispatch_workflow("o", "r", "ci.yml", "main"),
     ("POST", "/repos/o/r/actions/workflows/ci.yml/dispatches")),
    (lambda c: c.list_workflow_runs("o", "r"), ("GET", "/repos/o/r/actions/runs")),
    (lambda c: c.list_workflow_runs("o", "r", workflow="ci.yml"),
     ("GET", "/repos/o/r/actions/workflows/ci.yml/runs")),
    (lambda c: c.cancel_workflow_run("o", "r", 5),
     ("POST", "/repos/o/r/actions/runs/5/cancel")),
    (lambda c: c.rerun_failed_jobs("o", "r", 5),
     ("POST", "/repos/o/r/actions/runs/5/rerun-failed-jobs")),
    (lambda c: c.list_run_jobs("o", "r", 5), ("GET", "/repos/o/r/actions/runs/5/jobs")),
    (lambda c: c.list_artifacts("o", "r"), ("GET", "/repos/o/r/actions/artifacts")),
    (lambda c: c.list_artifacts("o", "r", run_id=5),
     ("GET", "/repos/o/r/actions/runs/5/artifacts")),
    (lambda c: c.search_code("foo repo:o/r"), ("GET", "/search/code")),
    (lambda c: c.search_issues("is:pr foo"), ("GET", "/search/issues")),
    (lambda c: c.rate_limit(), ("GET", "/rate_limit")),
])
def test_verbe_et_chemin_de_chaque_methode(cli, capture, appel, attendu):
    appel(cli)
    verbe, chemin = attendu
    assert capture["method"] == verbe
    assert capture["url"] == f"https://api.github.com{chemin}"


# --- encodage des paramètres ------------------------------------------------

def test_none_retire_booleens_et_listes(cli):
    got = dict(cli._encode_params(
        {"a": None, "b": True, "c": False, "labels": ["x", "y"], "d": 3}))
    assert "a" not in got
    assert got["b"] == "true" and got["c"] == "false"
    assert got["labels"] == "x,y" and got["d"] == 3
