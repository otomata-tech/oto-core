"""Aucun appel HTTP sortant ne doit pouvoir attendre indéfiniment.

Un appel sans `timeout` attend sans borne — ni `requests` ni `httpx` n'imposent de
défaut. Les clients synchrones tournent dans un threadpool borné côté serveur : la
boucle ne gèle pas, mais chaque appel pendu immobilise un worker à vie. Assez
d'amonts muets et le serveur cesse de servir TOUS les outils synchrones, sans
qu'aucune exception ne soit levée nulle part — une panne silencieuse, invisible.

⚠️ **Ce fichier a été VERT en ratant 17 appels** (oto-backend#867, 04/09/2026).

Sa version d'avant cherchait un MOTIF TEXTUEL, et ne reconnaissait que
`requests.<verbe>` et `session.<verbe>`. Elle ratait quatre formes sur six —
`requests.request(...)`, `httpx.<verbe>`, `client.<verbe>`, `self._client.<verbe>`.
Or les 17 appels nus étaient TOUS en `requests.request(...)` : exactement la forme
générique qu'emploient les clients qui centralisent leur transport, donc ceux qui
comptent le plus. Sa docstring parlait du « 17ᵉ appel sans timeout » comme d'une
hypothèse d'école ; il y en avait 17, et elle n'en voyait aucun.

La leçon tient en une phrase : **ce n'était pas la constante en tête de fichier qui
faisait croire que le sujet était traité, c'était ce test.** D'où la réécriture par
l'ARBRE, et surtout les épreuves du détecteur lui-même, plus bas — un garde-fou qui
ne tombe jamais ne garde rien, et personne ne s'en aperçoit puisqu'il est vert.
"""
from __future__ import annotations

import ast
import pathlib

_ROOT = pathlib.Path(__file__).resolve().parent.parent / "oto"

_VERBES = {"get", "post", "put", "patch", "delete", "request", "head", "options",
           "stream"}

# Kwargs qui trahissent un appel HTTP. Ils servent à écarter `d.get("clé")` : un
# accès de dictionnaire n'a ni en-têtes, ni corps, ni délai. Sans cette garde le
# détecteur signalait 10 accès de dictionnaire pour 1 vrai appel sur un module —
# et un garde-fou qui crie à tort finit ignoré, donc pire qu'absent.
_INDICES_HTTP = {"headers", "params", "json", "data", "timeout", "auth", "files",
                 "cookies", "content", "verify", "allow_redirects"}


def _porteur(appel: ast.Call) -> str | None:
    """Le nom sur lequel le verbe est appelé : `self.session.get` → `session`."""
    valeur = appel.func.value          # type: ignore[union-attr]
    if isinstance(valeur, ast.Attribute):
        return valeur.attr
    return valeur.id if isinstance(valeur, ast.Name) else None


def _est_un_appel_http(appel: ast.Call, porteur: str | None) -> bool:
    if porteur in ("requests", "httpx"):
        return True
    nom = (porteur or "").lower()
    if "session" not in nom and "client" not in nom:
        return False
    if (appel.func.attr == "get"                       # type: ignore[union-attr]
            and len(appel.args) <= 1
            and not ({k.arg for k in appel.keywords} & _INDICES_HTTP)):
        return False                                   # `d.get("clé")`
    return True


def appels_sans_delai(source: str, chemin: str = "<mémoire>") -> list[str]:
    """Appels HTTP de `source` qui ne passent aucun `timeout`.

    Fonction publique du module : les épreuves du détecteur, plus bas, l'appellent
    sur des cas fabriqués. Un détecteur qu'on ne peut pas éprouver hors du dépôt
    ne s'éprouve pas du tout."""
    if "requests" not in source and "httpx" not in source:
        return []
    try:
        arbre = ast.parse(source)
    except SyntaxError:
        return []
    fautes = []
    for n in ast.walk(arbre):
        if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)):
            continue
        if n.func.attr not in _VERBES:
            continue
        if not _est_un_appel_http(n, _porteur(n)):
            continue
        if "timeout" not in {k.arg for k in n.keywords}:
            fautes.append(f"{chemin}:{n.lineno}")
    return fautes


def _appels_nus_du_depot() -> list[str]:
    fautes = []
    for path in sorted(_ROOT.rglob("*.py")):
        src = path.read_text(encoding="utf-8", errors="replace")
        fautes += appels_sans_delai(src, str(path.relative_to(_ROOT.parent)))
    return fautes


# Cliquet, le temps de poser les 17 bornes (oto-backend#867). Il ne monte JAMAIS :
# chaque module borné le fait descendre, et il finit à 0 — moment où ce plafond
# disparaît et où le test redevient absolu. Il existe pour ne pas laisser le tronc
# rouge pendant la série de commits, pas pour tolérer un état.
_PLAFOND = 15


def test_aucun_appel_http_sans_delai():
    fautes = _appels_nus_du_depot()
    assert len(fautes) <= _PLAFOND, (
        f"{len(fautes)} appels HTTP sans `timeout` (plafond {_PLAFOND}) — un appel "
        "nu peut immobiliser un worker à vie :\n  " + "\n  ".join(fautes)
        + "\nAjouter `timeout=(connexion, lecture)`, p. ex. `timeout=_HTTP_TIMEOUT`.")


def test_le_plafond_ne_remonte_pas():
    """Le cliquet : dès que le compte descend, le plafond doit suivre. Sans ça il
    devient un budget de dette au lieu d'une trajectoire."""
    fautes = _appels_nus_du_depot()
    assert _PLAFOND <= 17, "le plafond ne remonte jamais"
    assert len(fautes) >= _PLAFOND - 3, (
        f"le compte réel ({len(fautes)}) est bien sous le plafond ({_PLAFOND}) : "
        "abaisser `_PLAFOND` à cette valeur, sinon le témoin cesse de mordre.")


# --- les épreuves du détecteur lui-même ------------------------------------
#
# C'est ce qui manquait à la version d'avant : elle était vert-aveugle et personne
# ne pouvait le voir. Un détecteur se prouve sur des cas dont on connaît la réponse.

def test_le_detecteur_voit_les_six_formes_d_appel():
    """Les quatre dernières étaient invisibles jusqu'au 04/09/2026, et c'est par
    la troisième que passaient les 17 appels réellement nus."""
    for forme in ("requests.get(u)",
                  "self.session.post(u)",
                  "requests.request(method, url)",
                  "httpx.post(u)",
                  "client.get(u, headers=h)",
                  "self._client.post(u)"):
        src = f"import requests\ndef f(u, h=None, method=None, url=None):\n    {forme}\n"
        assert appels_sans_delai(src) == ["<mémoire>:3"], f"forme ratée : {forme}"


def test_le_detecteur_laisse_passer_un_appel_borne():
    src = "import requests\ndef f(u):\n    requests.get(u, timeout=(10, 60))\n"
    assert appels_sans_delai(src) == []


def test_le_detecteur_accepte_une_constante_de_module():
    """La forme recommandée : `timeout=_HTTP_TIMEOUT`. Ne pas la reconnaître
    pousserait à écrire des valeurs en dur, l'inverse du but."""
    src = ("import requests\n_HTTP_TIMEOUT = (10, 60)\n"
           "def f(u):\n    requests.post(u, timeout=_HTTP_TIMEOUT)\n")
    assert appels_sans_delai(src) == []


def test_le_detecteur_ne_confond_pas_un_dictionnaire_avec_un_appel():
    """Sans cette garde, un module affichait 11 « appels nus » dont 10 étaient des
    accès de dictionnaire. Un garde-fou qui crie à tort finit ignoré."""
    src = ("import requests\n"
           "def f(node, s):\n"
           "    a = node.get('type')\n"
           "    b = s.get('fills', [{}])\n"
           "    return a, b\n")
    assert appels_sans_delai(src) == []


def test_le_detecteur_ne_regarde_pas_un_module_sans_client_http():
    """Pas d'import de client HTTP, pas d'appel HTTP : regarder ailleurs coûte du
    faux positif sans rien gagner."""
    src = "def f(d):\n    return d.get('x')\n"
    assert appels_sans_delai(src) == []
