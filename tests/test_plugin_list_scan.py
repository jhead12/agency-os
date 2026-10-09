"""
Tests for the "Scan a mailing list" page (plugins/pages/list_scan.py): reading
names and addresses out of OCR text, and importing them into a campaign.

Run: python -m pytest tests/
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import web.app as webapp  # noqa: E402
from core import access, plugin_pages  # noqa: E402
from plugins.pages.list_scan import ListScanPage, parse_entries, to_csv  # noqa: E402
from tests.test_access import client_for, db, make_user  # noqa: E402,F401


LABELS = """\
MAILING LIST - PAGE 1

JANE SMITH
123 MAIN ST APT 4
SPRINGFIELD, IL 62701
(217) 555-0101

2. John Doe, 45 Oak Ave, Peoria, IL 61602
3. Maria Lopez
PO Box 9
Saint Louis MO 63101-1234
maria@example.org

Chris Park
"""


# ── Parser ────────────────────────────────────────────────────────────


def test_reads_label_blocks_one_line_entries_and_contacts():
    entries, problems = parse_entries(LABELS)
    assert entries == [
        {"name": "Jane Smith", "address": "123 Main St Apt 4", "city": "Springfield", "state": "IL",
         "zip": "62701", "phone": "(217) 555-0101", "email": ""},
        {"name": "John Doe", "address": "45 Oak Ave", "city": "Peoria", "state": "IL",
         "zip": "61602", "phone": "", "email": ""},
        {"name": "Maria Lopez", "address": "PO Box 9", "city": "Saint Louis", "state": "MO",
         "zip": "63101-1234", "phone": "", "email": "maria@example.org"},
    ]
    # The header and a name with no address are reported, not imported.
    assert problems == ["MAILING LIST - PAGE 1", "Chris Park"]


def test_entries_need_a_name_and_a_real_state():
    entries, problems = parse_entries("123 Main St\nSpringfield, IL 62701\n\nAda Byron\n1 Elm St\nTown, ZZ 12345\n")
    assert entries == []
    assert problems == ["123 Main St / Springfield, IL 62701", "Ada Byron / 1 Elm St / Town, ZZ 12345"]


def test_reads_a_json_list():
    entries, problems = parse_entries(
        '[{"full_name": "Ann Lee", "street": "9 Pine Rd", "city": "Austin", "state": "tx", "zip": "78701"}, {"city": "x"}]')
    assert entries[0]["name"] == "Ann Lee" and entries[0]["state"] == "TX" and entries[0]["address"] == "9 Pine Rd"
    assert problems == ["item 2 has no name"]


def test_csv_uses_the_import_columns_and_defuses_formulas():
    entries, _ = parse_entries(LABELS)
    entries[1]["name"] = "=HYPERLINK(\"http://x\")"
    lines = to_csv(entries).splitlines()
    assert lines[0] == "name,address,city,state,zip,contact_name,contact_phone,contact_email,source"
    assert lines[1] == "Jane Smith,123 Main St Apt 4,Springfield,IL,62701,Jane Smith,(217) 555-0101,,list_scan"
    assert lines[2].startswith('"\'=HYPERLINK(""http://x"")",45 Oak Ave')


# ── Page ──────────────────────────────────────────────────────────────


def _page(monkeypatch):
    monkeypatch.setattr(plugin_pages, "_pages", {"list-scan": ListScanPage()})


def test_only_owners_can_open_or_post(db, monkeypatch):
    _page(monkeypatch)
    make_user(db, "rep@x.com", "Sales Rep")
    rep = client_for("rep@x.com")
    assert rep.get("/p/list-scan").status_code == 403
    assert rep.post("/p/list-scan", data={"text": LABELS, "action": "import", "campaign": "x"}).status_code == 403


def test_check_reports_without_importing(db, monkeypatch):
    _page(monkeypatch)
    make_user(db, "owner@x.com", access.OWNER_ROLE)
    client = client_for("owner@x.com")
    assert 'id="list-scan-form"' in client.get("/p/list-scan").text

    r = client.post("/p/list-scan", data={"text": LABELS, "action": "check"})
    assert "msg=Found%203%20entries" in r.headers["location"]
    assert db.conn.execute("SELECT COUNT(*) AS n FROM prospects").fetchone()["n"] == 0

    r = client.post("/p/list-scan", data={"text": "nothing here", "action": "check"})
    assert "error=No%20names" in r.headers["location"]


def test_download_csv_sends_a_file_named_after_the_upload(db, monkeypatch):
    _page(monkeypatch)
    make_user(db, "owner@x.com", access.OWNER_ROLE)
    r = client_for("owner@x.com").post(
        "/p/list-scan", data={"text": LABELS, "action": "csv", "filename": "Labels (Oct).pdf"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert r.headers["content-disposition"] == 'attachment; filename="Labels_Oct_.csv"'
    assert r.text.splitlines()[3].startswith("Maria Lopez,PO Box 9,Saint Louis,MO,63101-1234")
    assert db.conn.execute("SELECT COUNT(*) AS n FROM prospects").fetchone()["n"] == 0


def test_import_adds_leads_to_the_campaign_and_updates_on_reimport(db, monkeypatch):
    _page(monkeypatch)
    make_user(db, "owner@x.com", access.OWNER_ROLE)
    client = client_for("owner@x.com")
    campaign = webapp.get_campaigns()[0].db_name
    form = {"text": LABELS, "action": "import", "campaign": campaign, "filename": "labels.pdf"}

    assert "error=Pick%20a%20campaign" in client.post("/p/list-scan", data={**form, "campaign": ""}).headers["location"]

    r = client.post("/p/list-scan", data=form)
    assert "msg=Added%203%20new%20and%20updated%200" in r.headers["location"]
    rows = db.conn.execute(
        """SELECT p.name, p.address, p.city, p.state, p.zip, p.source, o.stage, o.contact_name, o.contact_phone,
                  o.contact_email, c.name AS campaign
           FROM prospects p JOIN outreach o ON o.prospect_id = p.id JOIN campaigns c ON c.id = o.campaign_id
           ORDER BY p.name""").fetchall()
    assert [(r["name"], r["zip"], r["campaign"], r["stage"], r["source"]) for r in rows] == [
        ("Jane Smith", "62701", campaign, "cold", "list_scan"),
        ("John Doe", "61602", campaign, "cold", "list_scan"),
        ("Maria Lopez", "63101-1234", campaign, "cold", "list_scan"),
    ]
    assert rows[0]["contact_phone"] == "(217) 555-0101" and rows[2]["contact_email"] == "maria@example.org"
    assert rows[1]["contact_name"] == "John Doe"

    r = client.post("/p/list-scan", data=form)
    assert "msg=Added%200%20new%20and%20updated%203" in r.headers["location"]
    assert db.conn.execute("SELECT COUNT(*) AS n FROM prospects").fetchone()["n"] == 3


def test_a_hidden_campaign_cant_be_picked(db, monkeypatch):
    _page(monkeypatch)
    make_user(db, "owner@x.com", access.OWNER_ROLE)
    r = client_for("owner@x.com").post(
        "/p/list-scan", data={"text": LABELS, "action": "import", "campaign": "no-such-campaign"})
    assert "error=Pick%20a%20campaign" in r.headers["location"]


def test_the_page_is_discovered():
    assert isinstance(plugin_pages.pages().get("list-scan"), ListScanPage)
