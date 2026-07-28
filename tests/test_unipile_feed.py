"""Tests for the Unipile LinkedIn home-feed parser (Voyager passthrough).

Le feed passe par la Magic Route raw data d'Unipile → Voyager (graphe normalisé
elements[]/included[]). Le mapping est défensif : ces tests verrouillent le contrat
de sortie ET le comportement de repli (item cassé, structure inattendue, curseur).
"""

from oto.tools.unipile.client import (
    _activity_urn_from,
    _posted_at_from_activity,
    _unpack_cursor,
    parse_feed,
)

SOCIAL_URN = "urn:li:fsd_socialDetail:(urn:li:activity:7234567890123456789,X)"


def _envelope(elements, included=None, token="TOK-1"):
    """Construit une enveloppe Unipile raw data {object, data:{data, included}}."""
    feed = {"elements": elements}
    if token is not None:
        feed["metadata"] = {"paginationToken": token}
    return {
        "object": "LinkedinRawData",
        "data": {
            "data": {"feedDashMainFeedByMainFeed": feed},
            "included": included or [],
        },
    }


def _update():
    return {
        "entityUrn": "urn:li:fsd_update:(urn:li:activity:7234567890123456789,MAIN_FEED)",
        "updateMetadata": {"urn": "urn:li:activity:7234567890123456789"},
        "actor": {
            "name": {"text": "Jane Doe"},
            "description": {"text": "CEO at Acme"},
            "subDescription": {"text": "2h"},
        },
        "commentary": {"text": {"text": "Hello world"}},
        "*socialDetail": SOCIAL_URN,
    }


def test_maps_a_full_update():
    resp = _envelope(
        [_update()],
        included=[
            {
                "entityUrn": SOCIAL_URN,
                "totalSocialActivityCounts": {
                    "numComments": 5,
                    "reactionTypeCounts": [{"count": 10}, {"count": 3}],
                },
            }
        ],
    )
    out = parse_feed(resp, count=20, start=0)
    assert out["count"] == 1
    item = out["items"][0]
    assert item["author_name"] == "Jane Doe"
    assert item["author_headline"] == "CEO at Acme"
    assert item["text"] == "Hello world"
    assert item["urn"] == "urn:li:activity:7234567890123456789"
    assert item["reactions_count"] == 13
    assert item["comments_count"] == 5
    assert item["post_url"] == (
        "https://www.linkedin.com/feed/update/urn:li:activity:7234567890123456789"
    )
    assert item["posted_at"] is not None  # décodé de l'id d'activité


def test_pagination_cursor_advances():
    resp = _envelope([_update()], token="TOK-NEXT")
    out = parse_feed(resp, count=20, start=40)
    assert out["cursor"] == "41|TOK-NEXT"


def test_no_token_means_no_cursor():
    resp = _envelope([_update()], token=None)
    assert parse_feed(resp, count=20)["cursor"] is None


def test_broken_item_is_dropped_not_fatal():
    resp = _envelope([{"garbage": True}, "not-a-dict", _update()])
    out = parse_feed(resp, count=20)
    # dict cassé + string → journalisés puis ignorés (plus de _raw verbeux) ;
    # l'update valide passe. Sortie propre = uniquement des posts mappés.
    assert all(not i.get("_unmapped") for i in out["items"])
    assert out["count"] == 1
    assert out["items"][0]["author_name"] == "Jane Doe"


def test_sponsored_and_promo_items_are_excluded():
    promo_inapp = {
        "entityUrn": "urn:li:fsd_update:(urn:li:inAppPromotion:20815,MAIN_FEED)",
        "content": {"promoComponent": {"title": {"text": "Hiring Pro"}}},
        "metadata": {"actionsPosition": "PROMO_COMPONENT"},
    }
    sponsored = {
        "entityUrn": "urn:li:fsd_update:(urn:li:activity:999,MAIN_FEED)",
        "actor": {"name": {"text": "BrandCo"}},
        "commentary": {"text": "ad"},
        "metadata": {"trackingData": {"sponsoredTracking": {"activityType": "SPONSORED"}}},
    }
    out = parse_feed(_envelope([promo_inapp, sponsored, _update()]), count=20)
    assert out["count"] == 1  # seul le post organique survit
    assert out["items"][0]["author_name"] == "Jane Doe"


def test_unexpected_structure_returns_raw():
    out = parse_feed({"data": {"data": {}}}, count=5)
    assert out["items"] == []
    assert out["count"] == 0
    assert "_raw" in out


def test_count_truncates_page():
    resp = _envelope([_update() for _ in range(10)])
    out = parse_feed(resp, count=3)
    assert out["count"] == 3


def test_surfacing_context_is_kept():
    """« X a commenté ceci » : la RAISON de remontée et son auteur sont conservés
    (feedback #280 — social selling par rebond)."""
    el = _update()
    el["header"] = {
        "text": {
            "text": "Sylvie Martin a commenté ceci",
            "attributes": [{"start": 0, "length": 13}],
        }
    }
    item = parse_feed(_envelope([el]), count=20)["items"][0]
    assert item["feed_reason"] == "Sylvie Martin a commenté ceci"
    assert item["surfaced_by"] == "Sylvie Martin"


def test_social_context_is_the_fallback_reason():
    el = _update()
    el["socialContext"] = {"text": "Paul Durand et 3 autres ont réagi"}
    item = parse_feed(_envelope([el]), count=20)["items"][0]
    assert item["feed_reason"] == "Paul Durand et 3 autres ont réagi"
    assert item["surfaced_by"] is None  # pas d'annotation → pas de nom inventé


def test_no_surfacing_context_means_none():
    item = parse_feed(_envelope([_update()]), count=20)["items"][0]
    assert item["feed_reason"] is None
    assert item["surfaced_by"] is None
    assert item["comment_authors"] == []


def test_comment_authors_from_social_detail():
    el = _update()
    el.pop("*socialDetail")
    el["socialDetail"] = {
        "comments": {"elements": [
            {"commenter": {"name": {"text": "Sylvie Martin"}}},
            {"commenter": {"name": {"text": "Paul Durand"}}},
            {"commenter": {"name": {"text": "Sylvie Martin"}}},  # dédupliqué
        ]}
    }
    item = parse_feed(_envelope([el]), count=20)["items"][0]
    assert item["comment_authors"] == ["Sylvie Martin", "Paul Durand"]


def test_comment_authors_from_included_comments_of_this_activity():
    """Les commentaires joints dans `included` portent l'id d'activité dans leur
    urn → rattachables au bon post ; ceux d'un autre post sont ignorés."""
    activity = "urn:li:activity:7234567890123456789"
    included = [
        {"entityUrn": f"urn:li:fsd_comment:(urn:li:comment:(activity:1,99),{activity})",
         "commenter": {"title": {"text": "Sylvie Martin"}}},
        {"entityUrn": "urn:li:fsd_comment:(urn:li:comment:(activity:2,98),urn:li:activity:42)",
         "commenter": {"title": {"text": "Autre Post"}}},
    ]
    item = parse_feed(_envelope([_update()], included=included), count=20)["items"][0]
    assert item["comment_authors"] == ["Sylvie Martin"]


def test_unpack_cursor_variants():
    assert _unpack_cursor(None) == (0, None)
    assert _unpack_cursor("") == (0, None)
    assert _unpack_cursor("20|abc") == (20, "abc")
    assert _unpack_cursor("plain-token") == (0, "plain-token")
    assert _unpack_cursor("bad|tok")[1] == "tok"


def test_activity_urn_extraction_from_composite_entity():
    el = {"entityUrn": "urn:li:fsd_update:(urn:li:activity:123,MAIN_FEED)"}
    assert _activity_urn_from(el) == "urn:li:activity:123"


def test_posted_at_decodes_timestamp_bits():
    # id = ts_ms << 22 → on doit retrouver un ISO8601 plausible
    ts_ms = 1_700_000_000_000
    aid = ts_ms << 22
    iso = _posted_at_from_activity(f"urn:li:activity:{aid}")
    assert iso is not None and iso.startswith("2023-")


def test_posted_at_rejects_garbage():
    assert _posted_at_from_activity("urn:li:activity:notanumber") is None
    assert _posted_at_from_activity(None) is None


# --- Contexte de remontée & repost (signal d'usage #280) ---------------------
# Le miroir ne gardait qu'auteur/texte/urn/compteurs. Or l'utilisateur se souvient
# d'un post par QUI l'a fait remonter (« untel a commenté ceci »), et sur un repost
# l'`author_name` est le re-partageur — l'auteur cherché disparaissait du miroir.

def test_feed_reason_est_capture():
    el = _update()
    el["header"] = {"text": {"text": "Marc Dupont a commenté ceci"}}
    out = parse_feed(_envelope([el]))
    assert out["items"][0]["feed_reason"] == "Marc Dupont a commenté ceci"


def test_repost_expose_l_auteur_original():
    el = _update()
    el["resharedUpdate"] = {"actor": {"name": {"text": "Sylvie Martin"}}}
    item = parse_feed(_envelope([el]))["items"][0]
    assert item["is_repost"] is True
    assert item["original_author_name"] == "Sylvie Martin"
    assert item["author_name"] == "Jane Doe", "l'auteur de surface reste le re-partageur"


def test_post_ordinaire_sans_contexte_ni_repost():
    item = parse_feed(_envelope([_update()]))["items"][0]
    assert item["feed_reason"] is None
    assert item["is_repost"] is False
    assert item["original_author_name"] is None


def test_reshared_imbrique_dans_content_est_vu_aussi():
    """Voyager range parfois le repartage sous `content` — les deux formes comptent."""
    el = _update()
    el["content"] = {"resharedUpdate": {"actor": {"name": {"text": "Léa Bernard"}}}}
    item = parse_feed(_envelope([el]))["items"][0]
    assert item["is_repost"] is True and item["original_author_name"] == "Léa Bernard"
