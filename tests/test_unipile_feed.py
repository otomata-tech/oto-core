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


def test_broken_item_is_kept_as_raw_not_fatal():
    resp = _envelope([{"garbage": True}, "not-a-dict", _update()])
    out = parse_feed(resp, count=20)
    # le dict cassé → _unmapped ; la string est ignorée ; l'update valide passe
    assert any(i.get("_unmapped") for i in out["items"])
    assert any(i.get("author_name") == "Jane Doe" for i in out["items"])


def test_unexpected_structure_returns_raw():
    out = parse_feed({"data": {"data": {}}}, count=5)
    assert out["items"] == []
    assert out["count"] == 0
    assert "_raw" in out


def test_count_truncates_page():
    resp = _envelope([_update() for _ in range(10)])
    out = parse_feed(resp, count=3)
    assert out["count"] == 3


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
