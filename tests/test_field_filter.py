"""Tests for the connector-response redaction engine (oto.tools.common.FieldFilter)."""

import pytest

from oto.tools.common import FieldFilter

try:
    import faker  # noqa: F401

    HAS_FAKER = True
except ImportError:
    HAS_FAKER = False


def test_empty_filter_is_identity():
    f = FieldFilter()
    data = {"a": 1, "nom": "Jean"}
    assert f.apply(data) == data


def test_input_is_never_mutated():
    src = {"secu": "123", "nested": [{"iban": "FR76123"}]}
    FieldFilter(rules=[{"fields": ["secu", "iban"], "action": "drop"}]).apply(src)
    assert src == {"secu": "123", "nested": [{"iban": "FR76123"}]}


def test_drop_and_remove_omit_key():
    for action in ("drop", "remove"):
        out = FieldFilter(rules=[{"fields": ["x"], "action": action}]).apply({"x": 1, "y": 2})
        assert out == {"y": 2}


def test_mask_keep_last_and_first():
    f = FieldFilter(rules=[{"fields": ["v"], "action": "mask", "keep_last": 4}])
    assert f.apply({"v": "FR7612340000"})["v"] == "••••0000"
    f2 = FieldFilter(rules=[{"fields": ["v"], "action": "mask", "keep_first": 2}])
    assert f2.apply({"v": "abcdef"})["v"] == "ab••••"


def test_mask_preserve_email():
    f = FieldFilter(rules=[{"fields": ["email"], "action": "mask", "preserve": "email"}])
    assert f.apply({"email": "jean.dupont@acme.com"})["email"] == "j••••@acme.com"
    # Not an email -> full mask, no leak.
    assert f.apply({"email": "notanemail"})["email"] == "••••"


def test_mask_preserve_phone_keeps_country_and_last_two():
    f = FieldFilter(rules=[{"fields": ["tel"], "action": "mask", "preserve": "phone"}])
    assert f.apply({"tel": "+33 6 12 34 56 21"})["tel"] == "+33 •••• 21"
    assert f.apply({"tel": "0612345621"})["tel"] == "•••• 21"


def test_mask_preserve_iban():
    f = FieldFilter(rules=[{"fields": ["iban"], "action": "mask", "preserve": "iban"}])
    assert f.apply({"iban": "FR76 1234 5678 9012 3456"})["iban"] == "FR••••3456"


def test_generalize_year_month_department_range():
    assert FieldFilter(rules=[{"fields": ["d"], "action": "generalize", "to": "year"}]).apply(
        {"d": "1985-03-12"}
    )["d"] == "1985"
    assert FieldFilter(rules=[{"fields": ["d"], "action": "generalize", "to": "month"}]).apply(
        {"d": "1985-03-12"}
    )["d"] == "1985-03"
    assert FieldFilter(rules=[{"fields": ["cp"], "action": "generalize", "to": "department"}]).apply(
        {"cp": "75011"}
    )["cp"] == "75"
    assert FieldFilter(
        rules=[{"fields": ["m"], "action": "generalize", "to": "range", "step": 1000}]
    ).apply({"m": 2450})["m"] == "2000-3000"


def test_hash_and_anonymize_are_deterministic_with_salt():
    f = FieldFilter(rules=[{"fields": ["n"], "action": "hash"}], salt="s")
    assert f.apply({"n": "x"})["n"] == f.apply({"n": "x"})["n"]
    a = FieldFilter(rules=[{"fields": ["n"], "action": "anonymize"}], salt="s")
    assert a.apply({"n": "x"})["n"].startswith("person_")


def test_unknown_action_fails_safe_to_mask():
    out = FieldFilter(rules=[{"fields": ["x"], "action": "wat"}]).apply({"x": "secret"})
    assert out["x"] == "••••"


def test_recurses_into_nested_lists_and_dicts():
    f = FieldFilter(rules=[{"fields": ["iban"], "action": "drop"}])
    out = f.apply({"accounts": [{"iban": "FR1", "label": "a"}, {"iban": "FR2", "label": "b"}]})
    assert out == {"accounts": [{"label": "a"}, {"label": "b"}]}


def test_matching_is_case_insensitive():
    f = FieldFilter(rules=[{"fields": ["iban"], "action": "drop"}])
    assert f.apply({"IBAN": "x", "y": 1}) == {"y": 1}


def test_none_value_is_preserved():
    f = FieldFilter(rules=[{"fields": ["x"], "action": "mask"}])
    assert f.apply({"x": None})["x"] is None


@pytest.mark.skipif(not HAS_FAKER, reason="Faker extra not installed")
def test_pseudonym_is_coherent_and_distinct():
    f = FieldFilter(rules=[{"fields": ["nom"], "action": "pseudonym", "kind": "name"}], salt="s")
    first = f.apply({"nom": "Jean Dupont"})["nom"]
    # Same source -> same pseudonym (coherent across the response).
    assert f.apply({"nom": "Jean Dupont"})["nom"] == first
    # Different source -> (almost surely) different pseudonym.
    assert f.apply({"nom": "Paul Martin"})["nom"] != first
    # The pseudonym is not the original value.
    assert first != "Jean Dupont"


@pytest.mark.skipif(not HAS_FAKER, reason="Faker extra not installed")
def test_pseudonym_unknown_kind_falls_back_to_name():
    f = FieldFilter(rules=[{"fields": ["x"], "action": "pseudonym", "kind": "wat"}], salt="s")
    out = f.apply({"x": "Jean"})["x"]
    assert out and out != "Jean"


def test_pseudonym_without_faker_raises_clear_error(monkeypatch):
    if HAS_FAKER:
        pytest.skip("Faker installed — cannot test the missing-extra path")
    f = FieldFilter(rules=[{"fields": ["x"], "action": "pseudonym"}])
    with pytest.raises(RuntimeError, match="oto-core\\[anonymize\\]"):
        f.apply({"x": "Jean"})
