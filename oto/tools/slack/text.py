"""Prétraitement du texte sortant Slack — pur, sans I/O.

Deux soucis distincts du même endroit (`SlackClient.post_message`) : ce que
Slack lit à tort dans le texte (`_escape_false_emoji_shortcodes`), et ce qui
dépasse sa longueur recommandée (`_chunk_text`). Modules frères plutôt que
noyés dans `client.py` (déjà > 500 lignes) — cf. CLAUDE.md du dépôt,
convention « parsing lourd sorti en module frère ».
"""
from __future__ import annotations

import re
from typing import List

# Slack lit tout `:jeton:` dont le jeton ne contient que des caractères de
# shortcode comme un emoji — MÊME quand ce n'est pas un nom connu. Sondé le
# 25/08 (oto-backend#711, signal #575) : "20:02 to 20:51: …" rend le second
# ":51:" en `{"type": "emoji", "name": "51"}`, avalant les deux chiffres et le
# `:` fermant — le `:` de "20:51" sert de délimiteur OUVRANT au `:` de
# ponctuation qui suit. Un jeton purement numérique n'est quasiment jamais un
# VRAI emoji Slack : les deux exceptions du jeu par défaut (`:100:` 💯,
# `:1234:` 🔢) sont allowlistées, tout le reste est cassé par une espace de
# largeur nulle — invisible à l'affichage, mais qui empêche Slack d'apparier
# les deux `:`.
_KNOWN_NUMERIC_EMOJI = {"100", "1234"}
_NUMERIC_SHORTCODE_RE = re.compile(r":(\d+):")
_ZERO_WIDTH_SPACE = "\u200b"  # échappé explicitement — jamais un caractère invisible littéral en source

# Limite RECOMMANDÉE par Slack pour `text` (docs chat.postMessage : "For best
# results, limit … to 4,000 characters"). Au-delà, Slack ne refuse rien : il
# TRONQUE en silence à 40 000 caractères, sans erreur ni indice pour la partie
# perdue — pire que refuser. `post_message` split donc lui-même avant ce seuil
# (oto-backend#711, signal #613) plutôt que de laisser Slack tronquer.
MAX_TEXT_LEN = 4000


def escape_false_emoji_shortcodes(text: str) -> str:
    """Empêche Slack de lire un nombre entre deux `:` (heure, ponctuation…)
    comme un shortcode emoji. Ne touche QUE les jetons purement numériques
    hors allowlist — un vrai shortcode nommé (`:smile:`, `:+1:`) traverse
    intact, seul le faux positif numérique est cassé."""
    def _break(m: "re.Match[str]") -> str:
        digits = m.group(1)
        if digits in _KNOWN_NUMERIC_EMOJI:
            return m.group(0)
        return ":" + _ZERO_WIDTH_SPACE + digits + ":"
    return _NUMERIC_SHORTCODE_RE.sub(_break, text)


def chunk_text(text: str, limit: int = MAX_TEXT_LEN) -> List[str]:
    """Découpe `text` en morceaux ≤ `limit`, en coupant sur un retour à la
    ligne ou une espace proche de la borne plutôt qu'en plein mot — coupe
    dure seulement si rien d'exploitable n'est trouvé dans la seconde moitié
    de la fenêtre."""
    if len(text) <= limit:
        return [text]
    chunks: List[str] = []
    rest = text
    while len(rest) > limit:
        cut = rest.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = rest.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    if rest:
        chunks.append(rest)
    return chunks
