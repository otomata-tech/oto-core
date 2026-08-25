"""Gardes du client Airtable — le wire format de la query string.

Née d'une revue (oto-core#57) : `requests` sérialise un booléen Python en
`"True"`/`"False"` dans la query string, qu'Airtable ne reconnaît pas — les
clients de référence (pyairtable, airtable.js) coercent en `true`/`1`. Dans un
corps JSON, en revanche, le booléen doit partir tel quel.
"""

from unittest.mock import patch

import pytest

from oto.tools.airtable.client import AirtableClient


@pytest.fixture()
def client():
    return AirtableClient(api_key="pat_test")


def _captured(client, method_name, *args, **kwargs):
    with patch.object(client, "_request", return_value={}) as req:
        getattr(client, method_name)(*args, **kwargs)
    return req.call_args


def test_list_records_query_bool_is_lowercase(client):
    call = _captured(
        client, "list_records", "appX", "Table", return_fields_by_field_id=True
    )
    assert call.kwargs["params"]["returnFieldsByFieldId"] == "true"


def test_get_record_query_bool_is_lowercase(client):
    call = _captured(
        client, "get_record", "appX", "Table", "recY", return_fields_by_field_id=False
    )
    assert call.kwargs["params"]["returnFieldsByFieldId"] == "false"


def test_query_bool_omitted_when_none(client):
    call = _captured(client, "list_records", "appX", "Table")
    assert "returnFieldsByFieldId" not in call.kwargs["params"]


def test_json_body_keeps_real_booleans(client):
    call = _captured(
        client,
        "create_records",
        "appX",
        "Table",
        [{"fields": {"Name": "Ada"}}],
        return_fields_by_field_id=True,
    )
    assert call.kwargs["json"]["returnFieldsByFieldId"] is True
