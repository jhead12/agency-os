"""
Campaign members and campaign Owners.

Campaign members: once a campaign has members (people, or everyone in a role),
only they and Owners see it and its leads, everywhere: lists, detail pages,
the call log, stats, AI tools. A campaign with no members works as before.

Campaign Owners: once a Super Admin assigns Owners to a campaign, other Owners
don't see it anywhere, settings and audit log included. Super Admins see all.

Run: TEST_DATABASE_URL=postgresql://localhost/agency_os_test python -m pytest tests/test_campaign_members.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import access, console, selling, tools  # noqa: E402
from core.access import AccessError  # noqa: E402
from tests.test_access import client_for, db, make_user, role_id  # noqa: E402,F401
from tests.test_recruiting import HEALTHCARE, RECRUITING, leads  # noqa: E402,F401


def user(db, email):
    return db.load_current_user(db.get_user_by_email(email)["id"])


def sees_dentist(db, email, leads) -> bool:
    """Every surface agrees on whether this user sees the healthcare campaign's lead."""
    c = client_for(email)
    views = {
        "list": "Bright Smile Dental" in c.get("/prospects").text,
        "detail": c.get(f"/prospects/{leads['dentist']}").status_code == 200,
        "campaigns": "Pasadena" in c.get("/campaigns").text,
        "stats": HEALTHCARE in {s.get("campaign") for s in c.get("/api/stats").json()},
        "ai": any(p["name"] == "Bright Smile Dental"
                  for p in tools.run_tool(db, user(db, email), "search_prospects", {}, source="t")["prospects"]),
    }
    assert len(set(views.values())) == 1, views
    return views["list"]


def test_no_members_means_everyone_as_before(db, leads):
    make_user(db, "rep@x.com", "Sales Rep")
    assert sees_dentist(db, "rep@x.com", leads)


def test_role_members_limit_a_campaign(db, leads):
    make_user(db, "rep@x.com", "Sales Rep")
    make_user(db, "caller@x.com", "Caller")
    make_user(db, "owner@x.com", access.OWNER_ROLE)
    assert db.add_campaign_member(HEALTHCARE, None, role_id=role_id(db, "Caller"))

    assert not sees_dentist(db, "rep@x.com", leads)
    assert client_for("rep@x.com").post(f"/prospects/{leads['dentist']}/stage", data={"stage": "engaged"},
                                        headers={"origin": "http://testserver"}).status_code == 404
    assert "Bright Smile" in client_for("caller@x.com").get("/prospects").text
    assert "Bright Smile" in client_for("owner@x.com").get("/prospects").text  # Owners see everything


def test_person_members_and_removing_the_last_one(db, leads):
    jane = make_user(db, "jane@x.com", "Sales Rep")
    make_user(db, "rep@x.com", "Sales Rep")
    db.add_campaign_member(HEALTHCARE, None, user_id=jane)
    assert sees_dentist(db, "jane@x.com", leads) and not sees_dentist(db, "rep@x.com", leads)
    assert db.add_campaign_member(HEALTHCARE, None, user_id=jane) is False  # already on it

    member = db.campaign_members(HEALTHCARE)[0]
    assert db.remove_campaign_member(HEALTHCARE, member["id"], None)
    assert sees_dentist(db, "rep@x.com", leads)  # no members: everyone again


def test_membership_does_not_bypass_requires_permission(db, leads):
    rep = make_user(db, "rep@x.com", "Sales Rep")
    db.add_campaign_member(RECRUITING, None, user_id=rep)
    assert "Avery Attorney" not in client_for("rep@x.com").get("/prospects").text  # still needs recruiting.view


def test_owners_manage_members_on_the_campaign_page(db, leads):
    make_user(db, "owner@x.com", access.OWNER_ROLE)
    caller = make_user(db, "caller@x.com", "Caller")
    c = client_for("owner@x.com")
    page = c.get(f"/admin/campaigns/{HEALTHCARE}").text
    assert "Who works this campaign" in page and "Everyone whose role allows it" in page

    r = c.post(f"/admin/campaigns/{HEALTHCARE}/members", data={"member": f"user:{caller}"})
    assert "members_msg=" in r.headers["location"]
    page = c.get(f"/admin/campaigns/{HEALTHCARE}").text
    assert "Members only" in page and "caller@x.com" in page
    assert "caller" in c.get("/admin/campaigns").text.split("Pasadena")[1][:400]

    member_id = db.campaign_members(HEALTHCARE)[0]["id"]
    r = c.post(f"/admin/campaigns/{HEALTHCARE}/members/{member_id}/delete")
    assert "everyone%20whose%20role%20allows%20it" in r.headers["location"]
    assert "members_error=" in c.post(f"/admin/campaigns/{HEALTHCARE}/members",
                                      data={"member": "user:999999"}).headers["location"]
    assert c.post("/admin/campaigns/nope/members", data={"member": f"user:{caller}"}).status_code == 404

    make_user(db, "rep@x.com", "Sales Rep")
    assert client_for("rep@x.com").post(f"/admin/campaigns/{HEALTHCARE}/members",
                                        data={"member": "user:1"}).status_code == 403
    actions = [r["action"] for r in db.list_audit()]
    assert "campaign.member_add" in actions and "campaign.member_remove" in actions


def test_console_commands(db, leads):
    make_user(db, "owner@x.com", access.OWNER_ROLE)
    make_user(db, "jane@x.com", "Sales Rep")
    owner = user(db, "owner@x.com")
    run = lambda line, ok=True: console.run_line(db, owner, line, confirmed=ok)  # noqa: E731

    assert "everyone whose role allows it" in run("campaigns members --campaign pasadena")["output"]
    assert run("campaigns assign --campaign pasadena --user jane@x.com", ok=False)["needs_confirmation"]
    out = run("campaigns assign --campaign pasadena --user jane@x.com")["output"]
    assert "Added jane@x.com" in out
    assert "already on it" in run("campaigns assign --campaign pasadena --user jane@x.com")["output"]
    run("campaigns assign --campaign pasadena --role Caller")
    members = run("campaigns members --campaign pasadena")["output"]
    assert "members only" in members and "Jane" in members.replace("jane", "Jane") and "Caller" in members

    assert "Give --user <email> or --role" in run("campaigns assign --campaign pasadena")["output"]
    assert "Campaigns:" in run("campaigns assign --campaign nowhere --user jane@x.com")["output"]
    assert "isn't a member" in run("campaigns unassign --campaign pasadena --user owner@x.com")["output"]
    run("campaigns unassign --campaign pasadena --role Caller")
    assert "No members left" in run("campaigns unassign --campaign pasadena --user jane@x.com")["output"]

    rep = user(db, "jane@x.com")
    assert "needs the Owner role" in console.run_line(db, rep, "campaigns members --campaign pasadena")["output"]


# ── Campaign Owners (assigned by a Super Admin) ────────────────────────


def test_assigned_owners_only(db, leads):
    a = make_user(db, "a@x.com", access.OWNER_ROLE)
    make_user(db, "b@x.com", access.OWNER_ROLE)
    make_user(db, "boss@x.com", access.SUPER_ADMIN_ROLE)
    make_user(db, "rep@x.com", "Sales Rep")
    boss = user(db, "boss@x.com")
    db.add_campaign_member(HEALTHCARE, None, role_id=role_id(db, "Sales Rep"))  # shows up in B's audit log otherwise
    assert db.add_campaign_owner(HEALTHCARE, a, boss)

    assert sees_dentist(db, "a@x.com", leads) and sees_dentist(db, "boss@x.com", leads)
    assert not sees_dentist(db, "b@x.com", leads)
    assert sees_dentist(db, "rep@x.com", leads)  # members are unaffected by Owner assignment

    b = client_for("b@x.com")
    assert "Pasadena" not in b.get("/admin/campaigns").text
    assert b.get(f"/admin/campaigns/{HEALTHCARE}").status_code == 404
    assert b.post(f"/admin/campaigns/{HEALTHCARE}/members", data={"member": "user:1"}).status_code == 404
    assert HEALTHCARE not in b.get("/admin/audit").text
    assert HEALTHCARE in client_for("a@x.com").get("/admin/audit").text
    out = console.run_line(db, user(db, "b@x.com"), "campaigns members --campaign pasadena")["output"]
    assert "No campaign matches" in out and HEALTHCARE not in out

    page = client_for("a@x.com").get(f"/admin/campaigns/{HEALTHCARE}").text
    assert "Owners of this campaign" in page and "a@x.com" in page and "Assign Owner" not in page

    owner_row = db.campaign_owners(HEALTHCARE)[0]
    assert db.remove_campaign_owner(HEALTHCARE, owner_row["id"], boss)
    assert sees_dentist(db, "b@x.com", leads)  # none assigned: every Owner again


def test_only_super_admins_assign_owners(db, leads):
    a = make_user(db, "a@x.com", access.OWNER_ROLE)
    rep = make_user(db, "rep@x.com", "Sales Rep")
    make_user(db, "boss@x.com", access.SUPER_ADMIN_ROLE)
    owner = user(db, "a@x.com")
    try:
        db.add_campaign_owner(HEALTHCARE, a, owner)
        raise AssertionError("an Owner assigned an Owner")
    except AccessError as e:
        assert "Only a Super Admin" in str(e)
    assert client_for("a@x.com").post(f"/admin/campaigns/{HEALTHCARE}/owners",
                                      data={"user_id": a}).status_code == 403
    assert "needs the Super Admin role" in console.run_line(
        db, owner, "campaigns add-owner --campaign pasadena --user a@x.com", confirmed=True)["output"]

    boss = client_for("boss@x.com")
    assert "Assign Owner" in boss.get(f"/admin/campaigns/{HEALTHCARE}").text
    r = boss.post(f"/admin/campaigns/{HEALTHCARE}/owners", data={"user_id": rep})
    assert "Owner%20role" in r.headers["location"]  # only Owners can be assigned
    r = boss.post(f"/admin/campaigns/{HEALTHCARE}/owners", data={"user_id": a})
    assert "Other%20Owners%20no%20longer" in r.headers["location"]
    assert "Owners: <strong>a</strong>" in boss.get("/admin/campaigns").text

    sa = user(db, "boss@x.com")
    out = console.run_line(db, sa, "campaigns owners --campaign pasadena")["output"]
    assert "assigned Owners only" in out and "a@x.com" in out
    out = console.run_line(db, sa, "campaigns remove-owner --campaign pasadena --user a@x.com", confirmed=True)
    assert "every Owner sees it again" in out["output"]
    actions = [r["action"] for r in db.list_audit()]
    assert "campaign.owner_add" in actions and "campaign.owner_remove" in actions


def test_packages_only_draw_from_campaigns_the_publisher_sees(db, leads):
    a = make_user(db, "a@x.com", access.OWNER_ROLE)
    b = make_user(db, "b@x.com", access.OWNER_ROLE)
    db.add_campaign_owner(HEALTHCARE, a, None)
    assert leads["dentist"] in selling.candidates(db, {}, hidden=selling.hidden_for_publisher(db, a))
    assert leads["dentist"] not in selling.candidates(db, {}, hidden=selling.hidden_for_publisher(db, b))
    # A publisher who's gone: only campaigns open to everyone.
    assert leads["attorney"] not in selling.candidates(db, {}, hidden=selling.hidden_for_publisher(db, None))
