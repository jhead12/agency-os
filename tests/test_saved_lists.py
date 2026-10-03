from types import SimpleNamespace
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from core import access
from core.db import Database
import web.app as webapp


def test_criteria_whitelist_and_pagination_reset():
    values = webapp.prospect_list_criteria({'q': 'arts & culture', 'cities': 'Los Angeles', 'page': 7, 'print': 1, 'user_id': 2, 'per_page': 'bad'})
    assert set(values) == set(webapp.SAVED_LIST_FIELDS)
    assert values['q'] == 'arts & culture'
    assert values['per_page'] == '50'
    assert webapp.prospect_list_criteria({'per_page': '9999'})['per_page'] == '500'


def test_save_and_delete_use_authenticated_owner(monkeypatch):
    db = MagicMock()
    monkeypatch.setattr(webapp, 'get_db', lambda: db)
    async def authorize(request: Request):
        request.state.user = SimpleNamespace(id=42)
    webapp.app.dependency_overrides[webapp.authorize] = authorize
    try:
        client = TestClient(webapp.app, follow_redirects=False)
        response = client.post('/prospects/saved-lists', data={'name': 'My arts list', 'q': 'arts & culture', 'user_id': 99, 'page': 8})
        assert response.status_code == 303
        args = db.save_prospect_list.call_args.args
        assert args[0:2] == (42, 'My arts list')
        assert 'user_id' not in args[2]
        assert parse_qs(urlsplit(response.headers['location']).query)['q'] == ['arts & culture']
        assert client.post('/prospects/saved-lists', data={'name': ' '}).status_code == 422
        client.post('/prospects/saved-lists/123/delete', data={'user_id': 99})
        db.delete_prospect_saved_list.assert_called_once_with(42, 123)
    finally:
        webapp.app.dependency_overrides.clear()


def test_saved_list_routes_require_prospect_view():
    for route in ('POST /prospects/saved-lists', 'POST /prospects/saved-lists/{list_id}/delete'):
        assert access.ROUTE_RULES[route] == 'prospects.view'


def test_saved_lists_are_private_and_same_name_updates(pg_url):
    db = Database(pg_url)
    db.install_access()
    owner = next(r['id'] for r in db.list_roles() if r['name'] == access.OWNER_ROLE)
    first = db.create_user('first@test.com', 'First', 'correct-horse-battery', [owner], actor=None)
    second = db.create_user('second@test.com', 'Second', 'correct-horse-battery', [owner], actor=None)
    db.save_prospect_list(first, 'Arts', {'q': 'arts'})
    saved_id = db.list_prospect_saved_lists(first)[0]['id']
    assert db.list_prospect_saved_lists(second) == []
    db.delete_prospect_saved_list(second, saved_id)
    assert len(db.list_prospect_saved_lists(first)) == 1
    db.save_prospect_list(first, 'Arts', {'q': 'music'})
    assert db.list_prospect_saved_lists(first)[0]['criteria'] == {'q': 'music'}
    assert len(db.list_prospect_saved_lists(first)) == 1
    db.save_prospect_list(second, 'Arts', {'q': 'different'})
    db.delete_prospect_saved_list(first, saved_id)
    assert db.list_prospect_saved_lists(first) == []
    assert len(db.list_prospect_saved_lists(second)) == 1
