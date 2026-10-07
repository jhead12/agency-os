"""
Customizable panels: each user's own arrangement of the prospect page.

Run: TEST_DATABASE_URL=postgresql://localhost/agency_os_test python -m pytest tests/test_panels.py
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import access, panels  # noqa: E402
from core.panels import LayoutError  # noqa: E402
from tests.test_access import client_for, db, make_user  # noqa: E402,F401
from tests.test_recruiting import leads  # noqa: E402,F401

HEADERS = {"X-AOS-Layout": "1"}
LAYOUT = {"background": "#1E1B4B",
          "panels": [{"id": "follow-up", "width": "two-thirds", "color": "#14532d"}, {"id": "org"}],
          "hidden": ["contact"]}


@pytest.mark.parametrize("layout, problem", [
    ({"panels": [{"id": "nope"}]}, "Unknown panel 'nope'"),
    ({"panels": [{"id": "org"}, {"id": "org"}]}, "listed twice"),
    ({"panels": [{"id": "org", "width": "huge"}]}, "width is one of"),
    ({"panels": [{"id": "org", "color": "red; background: url(x)"}]}, "colors look like"),
    ({"background": "javascript:1"}, "colors look like"),
    ({"panels": [{"id": "org"}], "hidden": ["org"]}, "both shown and hidden"),
    ({"script": 1}, "Unknown field 'script'"),
    ([], "is an object"),
])
def test_bad_layouts_say_what_to_fix(layout, problem):
    with pytest.raises(LayoutError, match=problem):
        panels.validate("prospect", layout)


def test_board_defaults_and_saved_layout():
    default = panels.Board("prospect", None)
    assert "last-contact" in default.hidden and "org" not in default.hidden
    assert 'data-width="full"' in default.attrs("calls")

    board = panels.Board("prospect", panels.validate("prospect", LAYOUT))
    assert board.order["follow-up"] == 0 and board.order["org"] == 1
    assert board.hidden >= {"contact", "last-contact"} and "follow-up" not in board.hidden
    assert "pipeline" not in board.hidden  # not mentioned: defaults, after the placed ones
    attrs = board.attrs("follow-up")
    assert 'data-width="two-thirds"' in attrs and "--panel-color: #14532d" in attrs and 'data-panel-tone="dark"' in attrs
    assert board.background == "#1e1b4b" and board.background_tone == "dark"
    assert panels.tone("#fef9c3") == "light"


def test_save_reload_and_reset_per_user(db, leads):
    make_user(db, "rep@x.com", "Sales Rep")
    make_user(db, "owner@x.com", access.OWNER_ROLE)
    rep = client_for("rep@x.com")
    url = f"/prospects/{leads['dentist']}"
    assert 'data-panel="last-contact"' in rep.get(url).text  # rendered, hidden, ready to add

    assert rep.post("/api/layouts/prospect", headers=HEADERS, json={"layout": LAYOUT}).json() == {"ok": True}
    page = rep.get(url).text
    assert "body { background: #1e1b4b; }" in page
    assert 'data-panel="contact"' in page and page.split('data-panel="contact"')[1].split(">")[0].count("hidden") == 1
    assert "#1e1b4b" not in client_for("owner@x.com").get(url).text  # only the rep's

    assert rep.post("/api/layouts/prospect", headers=HEADERS, json={"layout": None}).json()["ok"]
    assert "#1e1b4b" not in rep.get(url).text


def test_background_follows_the_stage_unless_the_user_picked_one(db, leads):
    make_user(db, "rep@x.com", "Sales Rep")
    rep = client_for("rep@x.com")
    url = f"/prospects/{leads['dentist']}"
    db.conn.execute("UPDATE outreach SET stage = 'engaged' WHERE id = ?", (leads["dentist_outreach"],))
    assert f"body {{ background: {panels.STAGE_BACKGROUNDS['engaged']}; }}" in rep.get(url).text
    db.conn.execute("UPDATE outreach SET stage = 'cold' WHERE id = ?", (leads["dentist_outreach"],))
    assert f"body {{ background: {panels.STAGE_BACKGROUNDS['cold']}; }}" in rep.get(url).text
    rep.post("/api/layouts/prospect", headers=HEADERS, json={"layout": {"background": "#1e1b4b"}})
    assert "body { background: #1e1b4b; }" in rep.get(url).text


def test_save_rejects_bad_requests(db):
    make_user(db, "rep@x.com", "Sales Rep")
    rep = client_for("rep@x.com")
    assert rep.post("/api/layouts/prospect", json={"layout": None}).status_code == 400  # header required
    r = rep.post("/api/layouts/prospect", headers=HEADERS, json={"layout": {"panels": [{"id": "nope"}]}})
    assert r.status_code == 400 and "Unknown panel" in r.json()["error"]
    assert "No customizable page" in rep.post("/api/layouts/dashboard", headers=HEADERS,
                                              json={"layout": None}).json()["error"]
    assert rep.post("/api/layouts/prospect", headers=HEADERS, content=b"not json").json()["error"] == "Body must be JSON"
