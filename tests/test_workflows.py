"""
Workflows: tutorials, users' own workflows, backup (export/import), and the
console commands that list, play and export them. The in-browser player itself
is exercised by a browser test.

Run: TEST_DATABASE_URL=postgresql://localhost/agency_os_test python -m pytest tests/test_workflows.py
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import access, console, workflows  # noqa: E402
from core.workflows import WorkflowError  # noqa: E402
from tests.test_access import client_for, db, make_user  # noqa: E402,F401
from tests.test_console import console_user  # noqa: E402

SIMPLE = """name: Morning check
description: Cold leads first.
steps:
  - goto: /prospects?stage=cold
    say: Today's cold leads.
  - highlight: table.data-table
  - Just a caption
  - pause: Your turn.
"""


def user(db, email):
    return db.load_current_user(db.get_user_by_email(email)["id"])


# ── Validation ─────────────────────────────────────────────────────────


def test_parse_accepts_every_step_kind():
    wf = workflows.parse("""name: All steps
steps:
  - goto: /prospects
  - say: Hello
  - highlight: .x
  - fill: {target: "input[name=q]", value: food}
  - click: "#go"
  - wait: 500
  - wait: {for: .detail-grid}
  - run: help
  - pause: Your turn
  - A bare string is a caption
""")
    assert [next(iter(s)) for s in wf["steps"]][-1] == "say"
    assert len(wf["steps"]) == 10


@pytest.mark.parametrize("step, problem", [
    ({"goto": "https://evil.example"}, "page of this app"),
    ({"goto": "//evil.example/x"}, "page of this app"),
    ({"goto": "/javascript:alert(1)"}, "page of this app"),
    ({"goto": "prospects"}, "page of this app"),
    ({"teleport": "/x"}, "unknown action 'teleport'"),
    ({"goto": "/a", "click": ".b"}, "put each action in its own step"),
    ({"fill": ".x"}, "fill must look like"),
    ({"wait": 999999}, "0-60000"),
    ({"run": "  "}, "needs a console command"),
    ({}, "is empty"),
])
def test_parse_rejects_bad_steps_with_a_fix(step, problem):
    with pytest.raises(WorkflowError, match=problem):
        workflows.validate({"name": "x", "steps": [step]})


def test_parse_rejects_bad_documents():
    with pytest.raises(WorkflowError, match="line 2"):
        workflows.parse("name: x\n  steps: [")
    with pytest.raises(WorkflowError, match="needs a name"):
        workflows.validate({"steps": ["hi"]})
    with pytest.raises(WorkflowError, match="list of steps"):
        workflows.validate({"name": "x"})
    with pytest.raises(WorkflowError, match="requires must be a permission"):
        workflows.validate({"name": "x", "requires": "everything", "steps": ["hi"]})
    with pytest.raises(WorkflowError, match="Unknown field 'script'"):
        workflows.validate({"name": "x", "steps": ["hi"], "script": "alert(1)"})


def test_every_tutorial_is_valid_and_visible_by_role(db):
    make_user(db, "boss@x.com", access.SUPER_ADMIN_ROLE)
    make_user(db, "caller@x.com", "Caller")
    make_user(db, "nobody@x.com")
    everyone = {t["slug"] for t in workflows.tutorials(user(db, "boss@x.com"))}
    assert everyone == {"tour", "find-a-prospect", "log-a-call", "console", "invite-a-teammate", "back-up-workflows",
                        "build-a-sellable-lead", "save-a-list", "sell-a-package", "your-data-royalties",
                        "pay-reps", "buy-a-package", "call-from-the-dashboard", "generate-a-lead-package",
                        "customer-accounts"}
    caller = {t["slug"] for t in workflows.tutorials(user(db, "caller@x.com"))}
    assert {"tour", "find-a-prospect", "log-a-call"} <= caller
    assert {"save-a-list", "your-data-royalties"} <= caller
    assert not {"console", "invite-a-teammate", "build-a-sellable-lead", "sell-a-package", "pay-reps",
                "buy-a-package", "call-from-the-dashboard", "generate-a-lead-package",
                "customer-accounts"} & caller  # no cli.use, prospects.edit or packages.*, not an owner
    assert {t["slug"] for t in workflows.tutorials(user(db, "nobody@x.com"))} == {"back-up-workflows"}


# ── Saving, playing, backup ────────────────────────────────────────────


def test_save_edit_play_and_delete_on_the_page(db):
    make_user(db, "rep@x.com", "Caller")
    c = client_for("rep@x.com")
    page = c.get("/workflows")
    assert page.status_code == 200 and "Tour of agency-os" in page.text and "None yet" in page.text

    r = c.post("/workflows/save", data={"definition": SIMPLE})
    assert r.status_code == 303 and r.headers["location"] == "/workflows?edit=morning-check&msg=Saved%20Morning%20check."
    page = c.get("/workflows?edit=morning-check")
    assert "Morning check" in page.text and "goto: /prospects?stage=cold" in page.text

    r = c.get("/api/workflows/mine/morning-check").json()
    assert r["ok"] and r["workflow"]["steps"][2] == {"say": "Just a caption"}
    assert c.get("/api/workflows/tutorial/tour").json()["workflow"]["name"] == "Tour of agency-os"
    assert c.get("/api/workflows/tutorial/console").status_code == 404  # not for a Caller

    bad = c.post("/workflows/save", data={"definition": "name: Broken\nsteps:\n  - goto: https://evil.example\n"})
    assert bad.status_code == 400 and "page of this app" in bad.text and "https://evil.example" in bad.text

    preview = c.post("/api/workflows/preview", headers={"X-AOS-Workflow": "1"}, json={"definition": SIMPLE})
    assert preview.json()["workflow"]["name"] == "Morning check"
    assert c.post("/api/workflows/preview", json={"definition": SIMPLE}).status_code == 400  # header required

    assert "msg=" in c.post("/workflows/morning-check/delete").headers["location"]
    assert c.get("/api/workflows/mine/morning-check").status_code == 404
    actions = [r["action"] for r in db.list_audit()]
    assert "workflow.save" in actions and "workflow.delete" in actions


def test_workflows_are_private(db):
    make_user(db, "a@x.com", "Caller")
    make_user(db, "b@x.com", "Caller")
    client_for("a@x.com").post("/workflows/save", data={"definition": SIMPLE})
    b = client_for("b@x.com")
    assert b.get("/api/workflows/mine/morning-check").status_code == 404
    assert "error=" in b.post("/workflows/morning-check/delete").headers["location"]
    assert workflows.get_mine(db, user(db, "a@x.com"), "morning-check") is not None


def test_backup_round_trip_to_another_user(db):
    make_user(db, "a@x.com", "Caller")
    make_user(db, "b@x.com", "Caller")
    a = client_for("a@x.com")
    a.post("/workflows/save", data={"definition": SIMPLE})
    a.post("/workflows/save", data={"definition": "name: Second\nsteps: [Hello]\n"})

    r = a.get("/workflows/export")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    backup = r.json()
    assert backup["format"] == "agency-os-workflows" and backup["version"] == 1
    assert [w["name"] for w in backup["workflows"]] == ["Morning check", "Second"]

    b = client_for("b@x.com")
    r = b.post("/workflows/import", files={"file": ("backup.json", json.dumps(backup), "application/json")})
    assert "Imported%202%20workflows" in r.headers["location"]
    assert [w["name"] for w in workflows.mine(db, user(db, "b@x.com"))] == ["Morning check", "Second"]

    # Importing again replaces by name instead of duplicating; a single YAML workflow imports too.
    b.post("/workflows/import", files={"file": ("backup.json", json.dumps(backup), "application/json")})
    b.post("/workflows/import", files={"file": ("one.yaml", "name: Third\nsteps: [Hi]\n", "text/yaml")})
    assert len(workflows.mine(db, user(db, "b@x.com"))) == 3


def test_bad_import_changes_nothing(db):
    make_user(db, "a@x.com", "Caller")
    c = client_for("a@x.com")
    backup = {"format": "agency-os-workflows", "version": 1, "workflows": [
        {"name": "Good", "steps": ["fine"]},
        {"name": "Evil", "steps": [{"goto": "https://evil.example"}]},
    ]}
    r = c.post("/workflows/import", files={"file": ("b.json", json.dumps(backup), "application/json")})
    assert "Nothing%20was%20imported" in r.headers["location"] and "Evil" in r.headers["location"]
    assert workflows.mine(db, user(db, "a@x.com")) == []
    r = c.post("/workflows/import", files={"file": ("x.bin", b"\x00{not yaml: [", "application/octet-stream")})
    assert "error=" in r.headers["location"]


# ── Console commands ───────────────────────────────────────────────────


def test_console_lists_plays_and_exports(db):
    console_user(db, "caller@x.com")
    me = user(db, "caller@x.com")
    workflows.save(db, me, workflows.parse(SIMPLE))

    listed = console.run_line(db, me, "workflows list")["output"]
    assert "tour" in listed and "morning-check" in listed and "yours" in listed

    played = console.run_line(db, me, 'workflows play --name "Morning check"')
    assert played["ok"] and played["play"]["name"] == "Morning check" and len(played["play"]["steps"]) == 4
    assert console.run_line(db, me, "workflows play --name tour")["play"]["name"] == "Tour of agency-os"

    remote = console.run_line(db, me, "workflows play --name tour", source="cli")
    assert "play" not in remote and "plays in the browser" in remote["output"]

    missing = console.run_line(db, me, "workflows play --name nope")
    assert not missing["ok"] and "No workflow named nope" in missing["output"]

    exported = json.loads(console.run_line(db, me, "workflows export")["output"])
    assert exported["workflows"][0]["name"] == "Morning check"
