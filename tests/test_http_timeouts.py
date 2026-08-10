"""Aucun appel HTTP sortant ne doit pouvoir attendre indéfiniment.

Un `requests.get` sans `timeout` attend sans borne. Les outils synchrones tournent
dans un threadpool borné côté serveur : la boucle ne gèle pas, mais chaque appel
pendu immobilise un worker à vie. Assez d'amonts muets et le serveur cesse de
servir TOUS les outils synchrones, sans qu'aucune exception ne soit levée nulle
part — une panne silencieuse, invisible à Sentry.

Ce test est une sonde STATIQUE : il relit le source des clients plutôt que
d'exécuter quoi que ce soit. C'est ce qui le rend utile — le 17ᵉ appel sans
timeout échouera ici, pas en production six mois plus tard.
"""
from __future__ import annotations

import pathlib
import re

_ROOT = pathlib.Path(__file__).resolve().parent.parent / "oto"

# `requests.get(...)` et les variantes à session (`self.session.post(...)`) — c'est
# l'oubli de cette seconde forme qui avait fait sous-estimer le compte de moitié.
_CALL = re.compile(
    r"\b(?:requests|self\.session|session|self\._session)\.(get|post|put|patch|delete)\s*\(")


def _call_source(src: str, start: int, open_paren: int) -> str | None:
    """Le texte de l'appel, de son nom à sa parenthèse fermante."""
    depth = 0
    for i in range(open_paren, min(len(src), open_paren + 4000)):
        if src[i] == "(":
            depth += 1
        elif src[i] == ")":
            depth -= 1
            if depth == 0:
                return src[start:i]
    return None


def _calls_without_timeout() -> list[str]:
    faults = []
    for path in sorted(_ROOT.rglob("*.py")):
        src = path.read_text(encoding="utf-8", errors="replace")
        for m in _CALL.finditer(src):
            call = _call_source(src, m.start(), m.end() - 1)
            if call is None or "timeout" in call:
                continue
            line = src[: m.start()].count("\n") + 1
            faults.append(f"{path.relative_to(_ROOT.parent)}:{line}")
    return faults


def test_no_outbound_http_call_without_timeout():
    faults = _calls_without_timeout()
    assert not faults, (
        "Appel HTTP sortant sans `timeout` — il peut immobiliser un worker à vie :\n  "
        + "\n  ".join(faults)
        + "\nAjoute `timeout=(connexion, lecture)`, p. ex. `timeout=_HTTP_TIMEOUT`."
    )
