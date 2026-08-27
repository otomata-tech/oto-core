"""Parsing du feed d'accueil LinkedIn (passthrough Voyager).

Extrait de `client.py` — contenu inchangé. `parse_feed` et les helpers privés
restent réexportés par `client.py` (chemin d'import figé, cf. les tests
`test_unipile_feed.py` qui importent `_activity_urn_from` & co. depuis là).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ---- feed parsing (Voyager graphe normalisé) ----------------------------
# Voyager renvoie un graphe NORMALISÉ : `data.feedDashMainFeedByMainFeed.elements[]`
# (les updates) + `data.included[]` (entités déréférencées par URN, ex. le
# socialDetail qui porte les compteurs). Le mapping est DÉFENSIF par conception :
# le schéma Voyager n'est pas contractuel, donc chaque champ est extrait en
# best-effort (accès imbriqué tolérant aux clés absentes) et un item qui casse
# le mapping est journalisé + renvoyé en mode dégradé plutôt que de tout faire
# échouer. Si la forme globale est inattendue, on remonte le payload brut.


def _unpack_cursor(cursor: Optional[str]) -> tuple[int, Optional[str]]:
    """Curseur opaque `"<start>|<paginationToken>"` → (start, token). Tolérant :
    cursor None/vide → (0, None) ; sans `|` → traité comme un token nu (start 0)."""
    if not cursor:
        return 0, None
    if "|" in cursor:
        start_s, token = cursor.split("|", 1)
        try:
            start = int(start_s)
        except (TypeError, ValueError):
            start = 0
        return start, (token or None)
    return 0, cursor


def _deep_get(obj: Any, *keys: str, default: Any = None) -> Any:
    """Accès imbriqué tolérant : retourne `default` dès qu'un maillon manque ou
    n'est pas un dict (jamais de KeyError/TypeError sur un graphe Voyager partiel)."""
    cur = obj
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur


def _text_of(node: Any) -> Optional[str]:
    """Voyager enveloppe souvent le texte dans `{text: "..."}` (parfois imbriqué).
    Accepte une string nue, `{text: str}` ou `{text: {text: str}}`."""
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        t = node.get("text")
        if isinstance(t, str):
            return t
        if isinstance(t, dict) and isinstance(t.get("text"), str):
            return t["text"]
    return None


def _activity_urn_from(el: dict) -> Optional[str]:
    """Extrait `urn:li:activity:<id>` d'un update Voyager.

    Pistes (dans l'ordre) : updateMetadata.urn / updateMetadata.shareUrn /
    le `entityUrn` de l'update (`urn:li:fsd_update:(urn:li:activity:...,...)`)."""
    for path in (("updateMetadata", "urn"), ("updateMetadata", "shareUrn")):
        v = _deep_get(el, *path)
        if isinstance(v, str) and "urn:li:activity:" in v:
            return _extract_activity(v)
    eu = el.get("entityUrn")
    if isinstance(eu, str):
        return _extract_activity(eu)
    return None


def _extract_activity(s: str) -> Optional[str]:
    """Isole `urn:li:activity:<id>` d'une chaîne (URN composé ou nu)."""
    marker = "urn:li:activity:"
    idx = s.find(marker)
    if idx < 0:
        return None
    rest = s[idx + len(marker):]
    digits = ""
    for ch in rest:
        if ch.isdigit():
            digits += ch
        else:
            break
    return f"{marker}{digits}" if digits else None


def _posted_at_from_activity(activity_urn: Optional[str]) -> Optional[str]:
    """Décode l'horodatage encodé dans l'id d'activité LinkedIn : les 41 bits de
    poids fort de l'id 64-bit = un timestamp en ms (`id >> 22`). Astuce robuste,
    indépendante du libellé relatif ('2h') affiché par Voyager."""
    if not activity_urn:
        return None
    try:
        aid = int(activity_urn.rsplit(":", 1)[-1])
    except (TypeError, ValueError):
        return None
    ms = aid >> 22
    # garde-fou : un epoch ms plausible (> 2001-09, < 2100)
    if not (1_000_000_000_000 < ms < 4_102_444_800_000):
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _social_counts(el: dict, included_by_urn: dict) -> tuple[Optional[int], Optional[int]]:
    """(reactions_count, comments_count) depuis le socialDetail — inliné ou
    déréférencé via `*socialDetail` dans `included`. Best-effort."""
    sd = el.get("socialDetail")
    if sd is None:
        ref = el.get("*socialDetail")
        if isinstance(ref, str):
            sd = included_by_urn.get(ref)
    counts = _deep_get(sd, "totalSocialActivityCounts", default={}) or {}
    comments = counts.get("numComments")
    reactions = None
    rtc = counts.get("reactionTypeCounts")
    if isinstance(rtc, list) and rtc:
        try:
            reactions = sum(int(r.get("count", 0)) for r in rtc if isinstance(r, dict))
        except (TypeError, ValueError):
            reactions = None
    if reactions is None:
        reactions = counts.get("numLikes")
    return reactions, comments


def _annotated_entity(node: Any) -> Optional[str]:
    """Nom de la PREMIÈRE entité annotée d'un texte Voyager. Voyager livre ses
    libellés en texte annoté — `{text: "Jean Dupont a commenté ceci", attributes:
    [{start, length, …}]}` — où la 1re annotation couvre l'acteur. On en découpe
    la tranche plutôt que de deviner par expression régulière (indépendant de la
    langue de l'interface). None si la forme n'est pas celle-là."""
    if not isinstance(node, dict):
        return None
    text = node.get("text")
    if isinstance(text, dict):  # `{text: {text, attributes}}`
        return _annotated_entity(text)
    attrs = node.get("attributes")
    if not isinstance(text, str) or not isinstance(attrs, list) or not attrs:
        return None
    first = attrs[0]
    if not isinstance(first, dict):
        return None
    start, length = first.get("start"), first.get("length")
    if not isinstance(start, int) or not isinstance(length, int) or length <= 0:
        return None
    name = text[start:start + length].strip()
    return name or None


def _feed_context(el: dict) -> tuple[Optional[str], Optional[str]]:
    """(feed_reason, surfaced_by) — POURQUOI ce post remonte dans MON feed.

    Un post d'inconnu apparaît presque toujours par REBOND d'une relation : « X a
    commenté ceci », « X a réagi », repartage. Cette raison est le cœur du social
    selling par rebond (qui de mon réseau interagit avec qui) et elle était perdue
    au mapping (feedback #280) : `feed_reason` = le libellé Voyager verbatim,
    `surfaced_by` = le nom de la relation à l'origine de la remontée.

    Best-effort : `header` (emplacement usuel du libellé de rebond) puis
    `socialContext`. Aucune des deux ⇒ (None, None) = post remonté directement."""
    for node in (el.get("header"), el.get("socialContext")):
        reason = _text_of(node)
        if reason:
            return reason, _annotated_entity(node)
    return None, None


def _comment_authors(el: dict, included_by_urn: dict,
                     activity_urn: Optional[str]) -> list[str]:
    """Auteurs des commentaires visibles sur cet update, dans l'ordre de rencontre.

    Le feed ne porte pas les commentaires complets, mais Voyager y joint les
    commentaires MIS EN AVANT (ceux qui font remonter le post) : à défaut du fil
    entier, garder QUI a commenté suffit à répondre « qui de mon réseau interagit
    avec qui » (feedback #280). Deux pistes : le `socialDetail` (inline ou
    déréférencé) puis les objets `comment` d'`included` rattachés à cette activité
    (leur `entityUrn` porte l'id d'activité). Best-effort, dédupliqué."""
    names: list[str] = []

    def _add(commenter: Any) -> None:
        if isinstance(commenter, str):  # référence `*commenter` → included
            commenter = included_by_urn.get(commenter)
        name = (_text_of(_deep_get(commenter, "name"))
                or _text_of(_deep_get(commenter, "title"))
                or _text_of(commenter))
        if name and name not in names:
            names.append(name)

    sd = el.get("socialDetail")
    if sd is None:
        ref = el.get("*socialDetail")
        if isinstance(ref, str):
            sd = included_by_urn.get(ref)
    for c in _deep_get(sd, "comments", "elements", default=[]) or []:
        if isinstance(c, dict):
            _add(c.get("commenter") or c.get("*commenter"))

    if activity_urn:
        for urn, obj in included_by_urn.items():
            if "comment" in urn.lower() and activity_urn in urn and isinstance(obj, dict):
                _add(obj.get("commenter") or obj.get("*commenter"))
    return names


# --- DE QUOI un post est fait (bloc `content` de l'update) -------------------
# Voyager range le média d'un post dans `content`, sous une clé qui NOMME le type de
# composant (`imageComponent`, `pollComponent`, `carouselContent`… — 42 noms relevés
# sur un feed réel). Ce bloc était intégralement jeté au mapping : un post à 2 775
# réactions dont le texte se réduit à « 🧐 » (tout le propos est dans l'image) devenait
# INCLASSABLE pour un agent — le post le plus engageant d'une page, invisible.
# Le bloc brut pèse ~4 700 caractères (images en 4 résolutions + tracking) : on n'en
# garde que le TYPE normalisé + l'intitulé porteur de sens quand il est là, ~100
# caractères. Le type est DÉRIVÉ du nom de la clé (suffixe `Component`/`Content`
# retiré, camelCase → snake_case), pas d'une table exhaustive à maintenir : un
# composant jamais vu rend son propre nom normalisé plutôt qu'un « unknown » muet.
# La table ci-dessous ne porte donc QUE les synonymes à replier.
_CONTENT_ALIASES = {
    "linked_in_video": "video",     # vidéo native LinkedIn
    "external_video": "video",      # YouTube & co. embarqués
    "native_video": "video",
    "slideshow": "carousel",        # diaporama d'images = un carrousel
}
# Ordre de DOMINANCE quand un update porte plusieurs composants : le premier de cette
# liste gagne. Classement par pouvoir de tri décroissant — ce qui appelle une action
# précise (sondage, document, article) avant le simple habillage (image). Un type
# inconnu passe après tous les connus (il informe, mais on ne sait pas encore combien).
_CONTENT_PRIORITY = ("poll", "document", "article", "newsletter", "event", "job",
                     "celebration", "video", "carousel", "image", "entity")
# Clés d'intitulé sondées sur le composant dominant (titre d'article, question d'un
# sondage, titre d'un document, texte alternatif d'une image…).
_CONTENT_LABEL_KEYS = ("title", "question", "headline", "name",
                       "altText", "accessibilityText")
_CONTENT_LABEL_MAX = 140   # borne le pire cas (≈ la limite d'une question de sondage)
_CAMEL_SPLIT = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def _content_key_to_type(key: str) -> Optional[str]:
    """`imageComponent` → `image`, `linkedInVideoComponent` → `video`,
    `carouselContent` → `carousel`. None si la clé n'est pas un composant de contenu
    (`resharedUpdate`, `$type`… restent hors du compte)."""
    for suffix in ("Component", "Content"):
        if key.endswith(suffix) and len(key) > len(suffix):
            base = _CAMEL_SPLIT.sub("_", key[: -len(suffix)]).lower()
            return _CONTENT_ALIASES.get(base, base)
    return None


def _content_label(node: Any) -> Optional[str]:
    """Intitulé porteur de sens d'un composant, s'il est disponible SANS COÛT (déjà
    dans la charge utile) : titre d'article, question de sondage, titre de document,
    texte alternatif d'une image. Sondé sur le composant, puis UN cran plus bas — ses
    sous-objets (`document.title`) et le 1er élément de ses listes
    (`images[0].accessibilityText`), là où Voyager range ces intitulés.
    Tronqué à `_CONTENT_LABEL_MAX`. None si le composant n'en porte pas."""
    if not isinstance(node, dict):
        return None
    candidates = [node]
    for value in node.values():
        if isinstance(value, dict):
            candidates.append(value)
        elif isinstance(value, list) and value and isinstance(value[0], dict):
            candidates.append(value[0])
    for obj in candidates:
        for key in _CONTENT_LABEL_KEYS:
            label = _text_of(obj.get(key))
            if isinstance(label, str) and label.strip():
                return label.strip()[:_CONTENT_LABEL_MAX]
    return None


def _content_facets(content: Any) -> tuple[str, Optional[str]]:
    """(content_type, content_title) d'un bloc `content` Voyager.

    Pas de bloc / aucun composant reconnaissable → `("text", None)` : le post ne porte
    que son texte, ce n'est pas un échec de mapping (un vrai schéma inattendu, lui,
    fait lever `_map_feed_item` et l'item est journalisé puis ignoré).
    Plusieurs composants → le DOMINANT (`_CONTENT_PRIORITY`, puis ordre d'apparition)
    donne le type ET l'intitulé : un champ scalaire reste filtrable à l'égalité en aval
    (miroir datastore), là où une liste ou un « image+article » ne l'est pas."""
    if not isinstance(content, dict):
        return "text", None
    found: list[tuple[int, int, str, Any]] = []
    for i, (key, node) in enumerate(content.items()):
        # ⚠️ Voyager déclare TOUTES les clés de son schéma GraphQL, la quasi-totalité à
        # `null` : la PRÉSENCE d'une clé ne dit rien, seule sa VALEUR compte. Sans ce
        # test, `dynamicPollComponent: null` faisait un sondage de n'importe quel post —
        # et `poll` étant en tête de la dominance, 48 posts sur 60 sont sortis en `poll`
        # au premier run réel (12/08). Une donnée fausse écrite à chaque sync est pire
        # que pas de donnée : l'agent trie dessus sans pouvoir en douter.
        if node is None or node == {} or node == [] or node == "":
            continue
        ctype = _content_key_to_type(key)
        if not ctype:
            continue
        rank = (_CONTENT_PRIORITY.index(ctype) if ctype in _CONTENT_PRIORITY
                else len(_CONTENT_PRIORITY))
        found.append((rank, i, ctype, node))
    if not found:
        return "text", None
    found.sort(key=lambda f: (f[0], f[1]))
    _, _, ctype, node = found[0]
    if ctype not in _CONTENT_PRIORITY:
        # Composant jamais vu : on rend son nom tel que Voyager le nomme (normalisé)
        # plutôt qu'un « unknown » muet — traçable quand LinkedIn en ajoute un.
        logger.debug("unipile feed: composant de contenu inconnu (%s)", ctype)
    return ctype, _content_label(node)


def _map_feed_item(el: dict, included_by_urn: dict) -> dict:
    """Un update Voyager → item normalisé. Lève si `el` n'est pas un update
    exploitable (ni actor ni commentary) — l'appelant gère le fallback."""
    actor = el.get("actor") if isinstance(el.get("actor"), dict) else {}
    commentary = el.get("commentary") if isinstance(el.get("commentary"), dict) else {}
    if not actor and not commentary:
        raise ValueError("element sans actor/commentary (pas un update feed)")

    activity_urn = _activity_urn_from(el)
    reactions, comments = _social_counts(el, included_by_urn)
    # POURQUOI ce post remonte dans MON feed (« Untel a commenté ceci », « Untel a
    # réagi ») : c'est le souvenir le plus fréquent de l'utilisateur — il se rappelle
    # QUI a fait remonter le post, pas son auteur. Sans ce champ, un post retrouvé
    # « par rebond » est introuvable dans le miroir (signal #280 : recherche d'un post
    # vu via le commentaire d'une relation → 0 résultat sur 710 posts miroir).
    # `_feed_context` lit `header` PUIS `socialContext` (repli) et rend aussi le NOM de
    # la relation à l'origine de la remontée — une lecture du seul `header` perdait les
    # deux.
    feed_reason, surfaced_by = _feed_context(el)
    post_url = (
        f"https://www.linkedin.com/feed/update/{activity_urn}"
        if activity_urn else None
    )
    # REPOST : `author_name` est alors le re-partageur et `text` son commentaire de
    # partage — l'auteur ORIGINAL, celui qu'on cherche, se perdait entièrement.
    reshared = el.get("resharedUpdate") if isinstance(el.get("resharedUpdate"), dict) else {}
    if not reshared:
        reshared = _deep_get(el, "content", "resharedUpdate", default={}) or {}
    reshared_actor = reshared.get("actor") if isinstance(reshared.get("actor"), dict) else {}
    # …et son COMMENTAIRE aussi : sur un repost, `text` porte le mot du re-partageur —
    # souvent vide ou « 👏 » — pendant que le contenu réel, celui sur lequel la règle de
    # tri veut juger, restait introuvable. Même traitement de type que le post porteur.
    reshared_commentary = (reshared.get("commentary")
                           if isinstance(reshared.get("commentary"), dict) else {})
    content_type, content_title = _content_facets(el.get("content"))
    return {
        "urn": activity_urn or el.get("entityUrn"),
        "author_name": _text_of(actor.get("name")),
        "author_headline": _text_of(actor.get("description")),
        "text": _text_of(commentary.get("text")) or _text_of(commentary),
        "posted_at": _posted_at_from_activity(activity_urn),
        "posted_relative": _text_of(actor.get("subDescription")),
        "reactions_count": reactions,
        "comments_count": comments,
        # Pourquoi ce post remonte + qui l'a fait remonter + qui a commenté
        # (feedback #280 : le rebond par une relation était perdu au mapping).
        "feed_reason": feed_reason,
        "surfaced_by": surfaced_by,
        "comment_authors": _comment_authors(el, included_by_urn, activity_urn),
        # DE QUOI le post est fait : sans ça, un post dont tout le propos est dans
        # l'image (texte = « 🧐 », 2 775 réactions) est inclassable — le type normalisé
        # + l'intitulé gratuit (titre d'article, question de sondage) le rendent triable
        # sans rapatrier le bloc `content` (~4 700 caractères, 93 % de tracking et de
        # miniatures). `content_type` vaut toujours quelque chose (`text` = post nu).
        "content_type": content_type,
        "content_title": content_title,
        "post_url": post_url,
        "is_repost": bool(reshared),
        "original_author_name": _text_of(reshared_actor.get("name")) or None,
        # Sur un repost, la substance est dans l'ORIGINAL : son texte et la nature de
        # son contenu. None hors repost (le champ reste présent : le miroir aval
        # projette des colonnes fixes).
        "original_text": (_text_of(reshared_commentary.get("text"))
                          or _text_of(reshared_commentary) or None) if reshared else None,
        "original_content_type": (_content_facets(reshared.get("content"))[0]
                                  if reshared else None),
    }


def _is_promo(el: dict) -> bool:
    """True si l'update est un encart sponsorisé/promotionnel (pub LinkedIn,
    « Hiring Pro », posts Promoted…) plutôt qu'un post organique — à exclure du
    feed. Plusieurs repères Voyager, best-effort : urn `inAppPromotion`, un
    `promoComponent` dans le contenu, `actionsPosition=PROMO_COMPONENT`, ou un
    bloc `sponsoredTracking` dans les métadonnées de tracking."""
    eu = el.get("entityUrn")
    if isinstance(eu, str) and "inAppPromotion" in eu:
        return True
    if _deep_get(el, "content", "promoComponent") is not None:
        return True
    if _deep_get(el, "metadata", "actionsPosition") == "PROMO_COMPONENT":
        return True
    if _deep_get(el, "metadata", "trackingData", "sponsoredTracking") is not None:
        return True
    return False


def parse_feed(resp: Any, count: int = 20, start: int = 0) -> dict:
    """Mappe l'enveloppe Unipile raw data du feed → `{items, cursor, count}`.

    Ne renvoie QUE des posts organiques normalisés : les encarts sponsorisés/promo
    (`_is_promo`) sont écartés silencieusement, et un update au schéma inattendu est
    **journalisé (warning) puis ignoré** (jamais de `_raw` verbeux dans la sortie).
    Si la structure globale est inattendue (pas d'`elements`), on remonte
    `{items: [], cursor: None, count: 0, _raw: resp}` + log error.
    """
    # Enveloppe Unipile {object, data} → JSON Voyager {data, included}.
    voyager = resp.get("data") if isinstance(resp, dict) else None
    feed = _deep_get(voyager, "data", "feedDashMainFeedByMainFeed")
    elements = feed.get("elements") if isinstance(feed, dict) else None
    if not isinstance(elements, list):
        logger.error(
            "unipile feed: structure inattendue (pas d'elements) — payload brut remonté"
        )
        return {"items": [], "cursor": None, "count": 0, "_raw": resp}

    included = _deep_get(voyager, "included", default=[])
    included_by_urn = {
        it["entityUrn"]: it
        for it in included
        if isinstance(it, dict) and isinstance(it.get("entityUrn"), str)
    }

    items: list[dict] = []
    for el in elements:
        if not isinstance(el, dict) or _is_promo(el):
            continue  # non-dict ou encart sponsorisé/promo → jamais renvoyé
        try:
            items.append(_map_feed_item(el, included_by_urn))
        except Exception:  # noqa: BLE001 — parsing défensif voulu
            logger.warning(
                "unipile feed: mapping d'un item échoué, ignoré", exc_info=True
            )
            continue

    items = items[:count]
    token = _deep_get(feed, "metadata", "paginationToken")
    next_cursor = f"{start + len(items)}|{token}" if token else None
    return {"items": items, "cursor": next_cursor, "count": len(items)}
