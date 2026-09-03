"""Contrat du client Productlane (v2, Bearer, pagination par curseur).

Mocke `requests.Session.request` : verbes et chemins relevés dans l'OpenAPI
publié par l'éditeur, en-tête d'auth, bornes de pagination, énumérations
**scopées à leur endpoint**, re-tentatives — et deux pièges que ce fichier
existe surtout pour verrouiller :

1. `iterate` boucle sur `page.has_more`, JAMAIS sur `data` non vide : la doc
   éditeur prévient qu'une page peut être vide « if it lined up », et s'arrêter
   là perdrait la suite silencieusement ;
2. le même nom de paramètre porte des valeurs différentes selon l'endpoint
   (`status` sur un fil ≠ `status` sur un brouillon de doc), donc une validation
   « par nom de paramètre » accepterait ce que l'amont refuse.
"""
from __future__ import annotations

import pytest

from oto.tools.common.errors import UpstreamHTTPError
from oto.tools.productlane import client as pl


class _Resp:
    def __init__(self, status_code: int = 200, body=None, headers=None):
        self.status_code = status_code
        self._body = body if body is not None else {"data": [], "page": {}}
        self.content = b"x"
        self.text = str(self._body)
        self.headers = headers or {}

    def json(self):
        return self._body


@pytest.fixture()
def capture(monkeypatch):
    seen = {"calls": []}

    def fake_request(self, method, url, **kwargs):
        seen.update(method=method, url=url, kwargs=kwargs)
        seen["calls"].append((method, url))
        return _Resp(200)

    monkeypatch.setattr(pl.requests.Session, "request", fake_request)
    monkeypatch.setattr(pl.time, "sleep", lambda _s: None)
    return seen


@pytest.fixture()
def cli():
    return pl.ProductlaneClient(api_key="pl_secret")


# --- authentification ------------------------------------------------------

def test_bearer_en_header_jamais_en_query(cli, capture):
    cli.list_threads()
    assert cli.session.headers["Authorization"] == "Bearer pl_secret"
    params = capture["kwargs"].get("params") or []
    assert "pl_secret" not in capture["url"]
    assert all("pl_secret" not in str(v) for _k, v in params)


def test_me_est_la_sonde_sans_scope(cli, capture):
    """`GET /me` est appelable par toute clé authentifiée : c'est ce qui permet
    de distinguer « clé invalide » (401) de « clé sans le droit » (403)."""
    cli.me()
    assert capture["method"] == "GET"
    assert capture["url"] == f"{cli.BASE_URL}/me"


# --- pagination par curseur -------------------------------------------------

@pytest.mark.parametrize("bad", [0, 201, 5000, -3])
def test_limit_hors_bornes_refusee_localement(cli, bad):
    with pytest.raises(ValueError, match="entre 1 et 200"):
        cli.list_threads(limit=bad)


@pytest.mark.parametrize("ok", [1, 50, 200])
def test_limit_dans_les_bornes_passe(cli, capture, ok):
    cli.list_threads(limit=ok)
    assert ("limit", ok) in capture["kwargs"]["params"]


def test_limit_booleen_refuse(cli):
    with pytest.raises(ValueError, match="doit être un entier"):
        cli.list_threads(limit=True)


def test_iterate_boucle_sur_has_more_pas_sur_data_vide(cli):
    """LE piège : une page intermédiaire VIDE avec `has_more: true` doit être
    suivie. Une boucle « tant que data » s'arrêterait là et perdrait la fin."""
    pages = [
        {"data": [{"id": 1}], "page": {"cursor": "c1", "has_more": True}},
        {"data": [], "page": {"cursor": "c2", "has_more": True}},      # vide !
        {"data": [{"id": 2}], "page": {"cursor": None, "has_more": False}},
    ]
    seen_cursors = []

    def fake_list(**kwargs):
        seen_cursors.append(kwargs.get("cursor"))
        return pages[len(seen_cursors) - 1]

    rows = list(cli.iterate(fake_list))
    assert [r["id"] for r in rows] == [1, 2]
    assert seen_cursors == [None, "c1", "c2"]


def test_iterate_sarrete_sans_curseur_meme_si_has_more(cli):
    """`has_more` vrai mais `cursor` nul : rien à demander de plus. Sans cette
    condition, la boucle redemanderait la même page à l'infini."""
    payload = {"data": [{"id": 1}], "page": {"cursor": None, "has_more": True}}
    assert list(cli.iterate(lambda **kw: payload)) == [{"id": 1}]


def test_iterate_respecte_max_pages(cli):
    payload = {"data": [{"id": 1}], "page": {"cursor": "c", "has_more": True}}
    assert len(list(cli.iterate(lambda **kw: payload, max_pages=3))) == 3


def test_iterate_refuse_un_curseur_fourni(cli):
    with pytest.raises(ValueError, match="gère le curseur"):
        list(cli.iterate(lambda **kw: {}, cursor="c1"))


def test_iterate_sur_un_retour_non_dict_ne_leve_pas(cli):
    assert list(cli.iterate(lambda **kw: None)) == []


# --- énumérations SCOPÉES à leur endpoint -----------------------------------

def test_status_de_fil_et_status_de_brouillon_ne_sont_pas_le_meme_jeu(cli, capture):
    """« accepted » est valide sur un brouillon de doc et invalide sur un fil.
    Une constante globale par nom de paramètre confondrait les deux."""
    cli.list_drafts(status="accepted")           # valide ici
    with pytest.raises(ValueError, match="Valeurs acceptées"):
        cli.list_threads(status="accepted")      # invalide là


def test_type_de_bloque_et_type_de_message_ne_sont_pas_le_meme_jeu(cli, capture):
    cli.list_blocked_senders(type="DOMAIN")      # EMAIL | DOMAIN
    with pytest.raises(ValueError, match="Valeurs acceptées"):
        cli.list_messages("t1", type="DOMAIN")   # email | slack | chat | ...


def test_visibility_all_est_un_filtre_jamais_une_ecriture(cli, capture):
    """`all` existe en filtre de liste, pas comme visibilité d'un article."""
    cli.list_articles(visibility="all")
    with pytest.raises(ValueError, match="Valeurs acceptées"):
        cli.create_article({"title": "t", "content": "c", "group_id": "g",
                            "visibility": "all"})


@pytest.mark.parametrize("niveau", pl.PAIN_LEVELS)
def test_tous_les_pain_levels_documentes_passent(cli, capture, niveau):
    cli.list_threads(pain_level=niveau)
    assert ("pain_level", niveau) in capture["kwargs"]["params"]


def test_pain_level_est_valide_aussi_a_la_creation(cli):
    with pytest.raises(ValueError, match="Valeurs acceptées"):
        cli.create_thread({"text": "t", "pain_level": "CRITIQUE",
                           "contact_email": "a@b.c"})


# --- expand : l'amont ignore, nous refusons ---------------------------------

def test_expand_accepte_une_liste_et_une_chaine(cli, capture):
    cli.get_thread("t1", expand=["messages", "comments"])
    assert ("expand", "messages,comments") in capture["kwargs"]["params"]
    cli.get_thread("t1", expand="messages")
    assert ("expand", "messages") in capture["kwargs"]["params"]


def test_expand_inconnu_est_refuse_localement(cli):
    """L'amont IGNORE une valeur inconnue : un fil reviendrait sans ses messages,
    en silence. Le refus local est ce qui rend la faute visible."""
    with pytest.raises(ValueError, match="Valeurs acceptées"):
        cli.get_thread("t1", expand="mesages")


# --- l'appel qui écrit à des tiers ------------------------------------------

def test_diffusion_sans_canal_est_refusee(cli):
    with pytest.raises(ValueError, match="au moins un canal"):
        cli.broadcast_changelog("c1")


def test_diffusion_par_email_part_bien(cli, capture):
    cli.broadcast_changelog("c1", email=True, subject="Nouveautés")
    assert capture["method"] == "POST"
    assert capture["url"] == f"{cli.BASE_URL}/changelogs/c1/broadcast"
    assert capture["kwargs"]["json"] == {"email": True, "subject": "Nouveautés"}


def test_diffusion_ne_touche_jamais_published(cli, capture):
    """Contrat éditeur explicite : « This endpoint never toggles `published` ».
    Le client ne doit donc pas glisser ce champ dans le corps."""
    cli.broadcast_changelog("c1", slack=True)
    assert "published" not in capture["kwargs"]["json"]


# --- garde-fous d'écriture ---------------------------------------------------

def test_fusion_dentreprise_exige_une_source_distincte(cli):
    with pytest.raises(ValueError, match="doit différer"):
        cli.merge_company("c1", "c1")
    with pytest.raises(ValueError, match="source_id"):
        cli.merge_company("c1", "")


def test_creation_detiquette_exige_les_quatre_champs(cli):
    with pytest.raises(ValueError, match="tag_group_id"):
        cli.create_tag("nom", "#fff", "Bug", "")


def test_import_de_fichier_exclut_url_et_base64(cli):
    with pytest.raises(ValueError, match="exclusifs"):
        cli.import_file(url="https://x/y.png", content_base64="AAA")
    with pytest.raises(ValueError, match="content_base64"):
        cli.import_file()


def test_relier_un_fil_exige_une_cible(cli):
    with pytest.raises(ValueError, match="issue_ids"):
        cli.link_thread("t1")


def test_etats_de_workflow_exigent_une_equipe(cli):
    with pytest.raises(ValueError, match="team_id"):
        cli.list_workflow_states("")


def test_portail_client_exige_un_email(cli):
    with pytest.raises(ValueError, match="email"):
        cli.get_customer_portal("")


def test_deplacer_des_articles_accepte_un_group_id_nul(cli, capture):
    """`group_id=None` DÉGROUPE : c'est une valeur signifiante, elle doit partir
    dans le corps et non être filtrée comme une absence."""
    cli.move_articles(["a1", "a2"], None)
    assert capture["kwargs"]["json"] == {"article_ids": ["a1", "a2"],
                                         "group_id": None}


# --- verbes et chemins ------------------------------------------------------

@pytest.mark.parametrize("appel,attendu", [
    (lambda c: c.list_threads(), ("GET", "/threads")),
    (lambda c: c.get_thread("t1"), ("GET", "/threads/t1")),
    (lambda c: c.create_thread({"text": "x", "contact_email": "a@b.c"}),
     ("POST", "/threads")),
    (lambda c: c.update_thread("t1", {"title": "y"}), ("PATCH", "/threads/t1")),
    (lambda c: c.delete_thread("t1"), ("DELETE", "/threads/t1")),
    (lambda c: c.list_messages("t1"), ("GET", "/threads/t1/messages")),
    (lambda c: c.send_message("t1", {"content": "hi"}),
     ("POST", "/threads/t1/messages")),
    (lambda c: c.list_comments("t1"), ("GET", "/threads/t1/comments")),
    (lambda c: c.post_comment("t1", "note"), ("POST", "/threads/t1/comments")),
    (lambda c: c.update_comment("t1", "k1", {"content": "z"}),
     ("PATCH", "/threads/t1/comments/k1")),
    (lambda c: c.delete_comment("t1", "k1"),
     ("DELETE", "/threads/t1/comments/k1")),
    (lambda c: c.link_thread("t1", issue_ids=["i1"]),
     ("POST", "/threads/t1/customer-needs")),
    (lambda c: c.list_contacts(), ("GET", "/contacts")),
    (lambda c: c.get_contact("p1"), ("GET", "/contacts/p1")),
    (lambda c: c.list_blocked_senders(), ("GET", "/contacts/blocked-senders")),
    (lambda c: c.block_sender("EMAIL", "a@b.c"),
     ("POST", "/contacts/blocked-senders")),
    (lambda c: c.unblock_sender("b1"),
     ("DELETE", "/contacts/blocked-senders/b1")),
    (lambda c: c.list_contact_companies("p1"), ("GET", "/contacts/p1/companies")),
    (lambda c: c.remove_contact_from_company("p1", "co1"),
     ("DELETE", "/contacts/p1/companies/co1")),
    (lambda c: c.list_companies(), ("GET", "/companies")),
    (lambda c: c.merge_company("co1", "co2"), ("POST", "/companies/co1/merge")),
    (lambda c: c.linear_customer_options(), ("GET", "/companies/linear-options")),
    (lambda c: c.list_projects(), ("GET", "/projects")),
    (lambda c: c.list_project_statuses(), ("GET", "/projects/statuses")),
    (lambda c: c.list_issues(), ("GET", "/issues")),
    (lambda c: c.list_workflow_states("team1"), ("GET", "/issues/workflow-states")),
    (lambda c: c.list_changelogs(), ("GET", "/changelogs")),
    (lambda c: c.list_changelog_tags(), ("GET", "/changelog-tags")),
    (lambda c: c.create_changelog_tag("n", color="#fff"), ("POST", "/changelog-tags")),
    (lambda c: c.delete_changelog_tag("g1"), ("DELETE", "/changelog-tags/g1")),
    (lambda c: c.list_articles(), ("GET", "/docs/articles")),
    (lambda c: c.move_articles(["a1"], "g1"), ("POST", "/docs/articles/move")),
    (lambda c: c.list_groups(), ("GET", "/docs/groups")),
    (lambda c: c.list_drafts(), ("GET", "/docs/drafts")),
    (lambda c: c.accept_draft("d1"), ("POST", "/docs/drafts/d1/accept")),
    (lambda c: c.decline_draft("d1"), ("POST", "/docs/drafts/d1/decline")),
    (lambda c: c.list_tags(), ("GET", "/tags")),
    (lambda c: c.list_tag_groups(), ("GET", "/tags/groups")),
    (lambda c: c.get_tag_group("tg1"), ("GET", "/tags/groups/tg1")),
    (lambda c: c.list_snippets(), ("GET", "/snippets")),
    (lambda c: c.list_snippet_folders(), ("GET", "/snippets/folders")),
    (lambda c: c.get_roadmap(), ("GET", "/portal/roadmap")),
    (lambda c: c.list_portal_instances(), ("GET", "/portal/instances")),
    (lambda c: c.import_file(url="https://x/y.png"), ("POST", "/files/import")),
    (lambda c: c.me(), ("GET", "/me")),
])
def test_verbe_et_chemin_de_chaque_methode(cli, capture, appel, attendu):
    appel(cli)
    verbe, chemin = attendu
    assert capture["method"] == verbe
    assert capture["url"] == f"{cli.BASE_URL}{chemin}"


def test_le_groupe_detiquettes_ne_collisionne_pas_avec_une_etiquette(cli, capture):
    """`/tags/groups` et `/tags/{id}` se ressemblent : vérifier que le groupe ne
    part pas comme une étiquette dont l'id serait « groups »."""
    cli.get_tag_group("tg1")
    assert capture["url"].endswith("/tags/groups/tg1")


# --- re-tentatives ----------------------------------------------------------

def test_le_429_est_retente_en_lecture(cli, monkeypatch):
    n = {"i": 0}

    def flaky(self, method, url, **kwargs):
        n["i"] += 1
        return _Resp(429, headers={"Retry-After": "0"}) if n["i"] < 3 else _Resp(200)

    monkeypatch.setattr(pl.requests.Session, "request", flaky)
    monkeypatch.setattr(pl.time, "sleep", lambda _s: None)
    cli.list_threads()
    assert n["i"] == 3


def test_une_ecriture_nest_jamais_retentee(cli, monkeypatch):
    """Aucune clé d'idempotence côté Productlane : rejouer un POST peut créer un
    doublon — ou, sur `broadcast`, ENVOYER LE COURRIER DEUX FOIS."""
    n = {"i": 0}

    def always_429(self, method, url, **kwargs):
        n["i"] += 1
        return _Resp(429, headers={"Retry-After": "0"})

    monkeypatch.setattr(pl.requests.Session, "request", always_429)
    monkeypatch.setattr(pl.time, "sleep", lambda _s: None)
    with pytest.raises(UpstreamHTTPError):
        cli.broadcast_changelog("c1", email=True)
    assert n["i"] == 1


def test_retry_after_est_respecte(cli, monkeypatch):
    dormi = []
    n = {"i": 0}

    def flaky(self, method, url, **kwargs):
        n["i"] += 1
        return _Resp(429, headers={"Retry-After": "11"}) if n["i"] == 1 else _Resp(200)

    monkeypatch.setattr(pl.requests.Session, "request", flaky)
    monkeypatch.setattr(pl.time, "sleep", lambda s: dormi.append(s))
    cli.list_threads()
    assert dormi == [11.0]


def test_erreur_amont_porte_le_nom_du_service(cli, monkeypatch):
    monkeypatch.setattr(
        pl.requests.Session, "request",
        lambda self, m, u, **k: _Resp(422, body={"error": {"code": "x"}}))
    with pytest.raises(UpstreamHTTPError) as exc:
        cli.list_threads()
    assert exc.value.status_code == 422 and exc.value.service == "productlane"


# --- encodage des paramètres ------------------------------------------------

def test_none_retire_et_booleens_serialises(cli):
    got = dict(cli._encode_params({"a": None, "b": True, "c": False, "d": 2}))
    assert "a" not in got
    assert got["b"] == "true" and got["c"] == "false" and got["d"] == 2


def test_une_liste_est_jointe_par_des_virgules(cli):
    assert cli._encode_params({"expand": ["messages", "comments"]}) == [
        ("expand", "messages,comments")]
