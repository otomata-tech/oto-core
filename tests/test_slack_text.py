"""`oto.tools.slack.text` — prétraitement pur du texte sortant (oto-backend#711).

Deux signaux de production distincts, même point d'entrée `post_message` :
- #575 : une heure `HH:MM` suivie d'un `:` de ponctuation ("20:51: la suite")
  se fait lire par Slack comme un shortcode emoji `:51:`, avalant les chiffres.
- #613 : un digest de ~4 000 caractères posté en UN appel a produit deux
  messages Slack distincts, et l'appelant n'a récupéré qu'un seul `ts` —
  aucun moyen de savoir qu'un second message existait ni de le relier.
"""
from oto.tools.slack.text import chunk_text, escape_false_emoji_shortcodes


def test_clock_time_followed_by_punctuation_colon_is_escaped():
    # Cas exact du signal #575 (session e836f3f7, 25/08) : le `:` de ponctuation
    # après "51" referme le `:` de "20:51" en un faux shortcode `:51:`.
    text = "20:02 to 20:51: one Partoo voice asked"
    out = escape_false_emoji_shortcodes(text)
    assert ":51:" not in out              # le motif qui déclenchait le faux shortcode a disparu
    assert "51" in out                    # les chiffres ne sont plus avalés
    assert "20:02" in out                 # la première heure, jamais ambiguë, reste intacte


def test_escaped_text_is_visually_identical_once_zero_width_space_stripped():
    text = "20:02 to 20:51: one Partoo voice asked"
    out = escape_false_emoji_shortcodes(text)
    assert out.replace("\u200b", "") == text


def test_named_shortcode_is_untouched():
    # Un vrai emoji nommé n'est pas un faux positif numérique : intact.
    text = "great job :smile: and :+1: on this"
    assert escape_false_emoji_shortcodes(text) == text


def test_known_numeric_emoji_are_allowlisted():
    # :100: (💯) et :1234: (🔢) sont de VRAIS shortcodes Slack malgré leur nom
    # tout en chiffres — on ne les casse pas.
    text = "score :100: nice, and :1234: for numbers"
    assert escape_false_emoji_shortcodes(text) == text


def test_unrelated_colons_are_untouched():
    text = "see https://example.com/a:b and note: nothing weird here"
    assert escape_false_emoji_shortcodes(text) == text


def test_short_text_is_not_chunked():
    text = "hello world"
    assert chunk_text(text) == [text]


def test_text_at_exact_limit_is_not_chunked():
    text = "x" * 4000
    assert chunk_text(text, limit=4000) == [text]


def test_long_text_is_split_on_word_boundary():
    # Pas de coupe en plein mot : chaque morceau reste ≤ limite.
    text = ("mot " * 2000).strip()          # ~9999 caractères
    parts = chunk_text(text, limit=100)
    assert all(len(p) <= 100 for p in parts)
    assert "".join(parts).replace("  ", " ").strip() != ""  # rien perdu de substantiel
    rejoined = " ".join(p.strip() for p in parts)
    assert rejoined.split() == text.split()  # aucun mot perdu ni tronqué


def test_long_text_without_boundary_hard_cuts():
    text = "a" * 250
    parts = chunk_text(text, limit=100)
    assert parts == ["a" * 100, "a" * 100, "a" * 50]
