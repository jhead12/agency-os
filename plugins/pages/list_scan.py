"""Scan a mailing list: turn a scanned or typed list of names and addresses into campaign leads.

A port of u9itus's OCR candidate import (u9itus.dev
app/Services/OcrCandidateImportService.php) for mailing lists. The steps are
the same: use a PDF's text layer if it has one, else OCR the page with
Tesseract, then read records out of the text (JSON and plain text work too).
Two things differ:

- The text is extracted in the browser (static/list_scan.js, with pdf.js and
  tesseract.js), because page plugins don't receive uploaded files. Only the
  text is posted here. The file itself never reaches the server.
- The parser reads name-and-address blocks instead of "Name - Party" lines.

What the parser reads (see parse_entries):

    Jane Smith                      one entry per block: a name line,
    123 Main St                     one or more street lines,
    Springfield, IL 62701           and a "City, ST 12345" line that ends it

    Jane Smith, 123 Main St, Springfield, IL 62701     or all on one line

A phone number or email address inside a block goes on the lead's outreach row.
"""

import csv
import hashlib
import io
import json
import re

from core.models import Prospect
from core.plugin_pages import Download

SOURCE = "list_scan"
MAX_TEXT = 500_000    # characters; a few hundred pages of OCR text
MAX_ENTRIES = 5_000

STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL", "IN", "IA",
    "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT",
    "VA", "WA", "WV", "WI", "WY", "DC", "PR",
}

_STATE_ZIP = r",?\s+(?P<state>[A-Za-z]{2})\.?\s+(?P<zip>\d{5}(?:-\d{4})?)$"
# "Springfield, IL 62701"
CITY_STATE_ZIP = re.compile(r"^(?P<city>[A-Za-z][A-Za-z .'\-]*?)" + _STATE_ZIP)
# "123 Main St, Springfield, IL 62701", or with the name in front
STREET_CITY_STATE_ZIP = re.compile(r"^(?P<street>.+?),\s*(?P<city>[A-Za-z][A-Za-z .'\-]*?)" + _STATE_ZIP)
EMAIL = re.compile(r"^(?:e-?mail\s*[:.]?\s*)?(?P<email>[\w.+-]+@[\w-]+(?:\.[\w-]+)+)$", re.I)
PHONE = re.compile(r"^(?:(?:tel|phone|ph)\s*[:.]?\s*)?(?P<phone>\+?(?:1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4})$", re.I)
# List numbering ("1.", "12)") but not a street number ("123 Main St")
NUMBERING = re.compile(r"^\s*\d{1,4}[.)]\s+")
STREETISH = re.compile(r"^(?:\d|p\.?\s*o\.?\s*box\b)", re.I)
# Kept in capitals when an all-caps line is turned into Title Case
UPPER_WORDS = {"PO", "NE", "NW", "SE", "SW", "LLC", "LLP", "PC", "PLLC", "II", "III", "IV", "USA"}


def normalize_line(line: str) -> str:
    line = NUMBERING.sub("", line)
    return re.sub(r"\s+", " ", line).strip()


def display(value: str) -> str:
    """Title Case for ALL-CAPS text (common on labels), else unchanged."""
    if not value.isupper():
        return value
    return " ".join(w if w.strip(".,") in UPPER_WORDS else w.capitalize() for w in value.split(" "))


def _entry(name: str, street: list[str], city: str, state: str, zip_code: str,
           phone: str = "", email: str = "") -> dict:
    return {
        "name": display(name.strip(" ,")),
        "address": display(", ".join(s.strip(" ,") for s in street if s.strip(" ,"))),
        "city": display(city.strip()),
        "state": state.upper(),
        "zip": zip_code,
        "phone": phone,
        "email": email,
    }


def _json_entries(text: str) -> tuple[list[dict], list[str]]:
    """A JSON list of {name, address, city, state, zip, phone, email} objects."""
    try:
        rows = json.loads(text)
    except ValueError:
        return [], ["The text starts with [ but isn't valid JSON."]
    entries, problems = [], []
    for i, row in enumerate(rows if isinstance(rows, list) else []):
        if not isinstance(row, dict):
            continue
        get = lambda *keys: next((str(row[k]).strip() for k in keys if row.get(k)), "")  # noqa: E731
        name = get("name", "full_name")
        if not name:
            problems.append(f"item {i + 1} has no name")
            continue
        state = get("state").upper()
        entries.append(_entry(name, [get("address", "street", "address1")], get("city"),
                              state if state in STATES else "", get("zip", "zip_code", "postal_code"),
                              get("phone"), get("email")))
    return entries, problems


def parse_entries(text: str) -> tuple[list[dict], list[str]]:
    """The entries in the text, and the lines that couldn't be read as part of one."""
    text = (text or "").strip()
    if text.startswith("["):
        return _json_entries(text)

    entries: list[dict] = []
    problems: list[str] = []
    block: list[str] = []
    contact = {"phone": "", "email": ""}

    def drop_block():
        if block:
            problems.append(" / ".join(block))
        block.clear()
        contact.update(phone="", email="")

    def finish(street_tail: list[str], m) -> None:
        """The block plus this line make one entry: name first, then street lines."""
        lines = block + street_tail
        if len(lines) < 2:
            problems.append(" / ".join(lines + [m.group(0)]))
        else:
            entries.append(_entry(lines[0], lines[1:], m["city"], m["state"], m["zip"], **contact))
        block.clear()
        contact.update(phone="", email="")

    for raw in text.splitlines():
        line = normalize_line(raw)
        if not line:
            drop_block()
            continue
        m = EMAIL.match(line) or PHONE.match(line)
        if m:
            field, value = next((k, v) for k, v in m.groupdict().items() if v)
            # Right after an entry's last line, it belongs to that entry; otherwise to the one being read.
            if not block and entries and not entries[-1][field]:
                entries[-1][field] = value
            else:
                contact[field] = value
            continue

        m = CITY_STATE_ZIP.match(line)
        if m and m["state"].upper() in STATES:
            finish([], m)
            continue

        m = STREET_CITY_STATE_ZIP.match(line)
        if m and m["state"].upper() in STATES:
            street = m["street"]
            if not block and "," in street and not STREETISH.match(street):
                # "Jane Smith, 123 Main St, ..." on one line
                name, street = street.split(",", 1)
                block.append(name)
            finish([street], m)
            continue

        block.append(line)

    drop_block()
    return entries, problems


def entry_ref(entry: dict) -> str:
    """A stable id for the person at this address, so re-importing updates instead of duplicating
    and two people with the same name in one state stay two prospects."""
    key = "|".join(re.sub(r"\W+", "", entry[k].lower()) for k in ("name", "address", "zip"))
    return hashlib.sha256(key.encode()).hexdigest()[:20]


# Header names the campaign CSV import (web/app.py, _CSV_FIELD_MAP) reads, so the file can go back in there.
CSV_COLUMNS = [("name", "name"), ("address", "address"), ("city", "city"), ("state", "state"), ("zip", "zip"),
               ("contact_name", "name"), ("contact_phone", "phone"), ("contact_email", "email")]
# A cell a spreadsheet would run as a formula; "+1 555…" and "-" followed by a digit are left alone.
FORMULA = re.compile(r"^(?:[=@\t\r]|[+-](?![\d(]))")


def to_csv(entries: list[dict]) -> str:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow([header for header, _ in CSV_COLUMNS] + ["source"])
    for e in entries:
        cells = [e[field] for _, field in CSV_COLUMNS] + [SOURCE]
        writer.writerow(["'" + c if FORMULA.match(c) else c for c in cells])
    return out.getvalue()


def _summary(problems: list[str]) -> str:
    if not problems:
        return ""
    return f" {len(problems)} part(s) couldn't be read, first: \"{problems[0][:120]}\"."


class ListScanPage:
    key = "list-scan"
    title = "Scan a mailing list"
    permission = "@owner"  # adds leads to campaigns, like the campaign CSV import
    template = "list_scan.html"

    def context(self, page):
        return {"campaigns": page.campaigns, "max_text": MAX_TEXT}

    def post(self, page, form):
        text = form.get("text") or ""
        if len(text) > MAX_TEXT:
            raise ValueError(f"That's more than {MAX_TEXT:,} characters. Split the list into smaller files.")
        entries, problems = parse_entries(text)
        if not entries:
            raise ValueError("No names and addresses found. Each entry needs a line like "
                             "\"Springfield, IL 62701\" after its name and street." + _summary(problems))
        if len(entries) > MAX_ENTRIES:
            raise ValueError(f"Found {len(entries):,} entries; import at most {MAX_ENTRIES:,} at a time.")

        if form.get("action") == "csv":
            stem = re.sub(r"\.[^.]*$", "", form.get("filename") or "") or "mailing-list"
            return Download(f"{stem}.csv", to_csv(entries))

        if form.get("action") != "import":
            first = entries[0]
            return (f"Found {len(entries)} entr{'y' if len(entries) == 1 else 'ies'}, e.g. "
                    f"{first['name']}, {first['address']}, {first['city']}, {first['state']} {first['zip']}."
                    + _summary(problems))

        campaign = next((c for c in page.campaigns if c.db_name == form.get("campaign")), None)
        if campaign is None:
            raise ValueError("Pick a campaign.")

        db = page.db
        campaign_id = db.get_campaign_id(campaign.db_name) or db.upsert_campaign(
            campaign.db_name, str(campaign.config_dir))
        added = updated = 0
        errors = []
        for e in entries:
            prospect = Prospect(
                name=e["name"], address=e["address"] or None, city=e["city"] or None,
                state=e["state"] or None, zip=e["zip"] or None, source=SOURCE,
                metadata={"external_ref": entry_ref(e)},
            )
            try:
                existed = db.find_prospect(prospect) is not None
                outreach_id = db.upsert_outreach(db.upsert_prospect(prospect), campaign_id)
                db.update_outreach(outreach_id, {"contact_name": e["name"],
                                                 **{f"contact_{k}": e[k] for k in ("phone", "email") if e[k]}})
            except Exception as exc:
                errors.append(f"{e['name']}: {exc}")
                continue
            if existed:
                updated += 1
            else:
                added += 1

        db.audit(page.user, "list_scan.import", "campaign", campaign.db_name, {
            "file": (form.get("filename") or "")[:200], "added": added, "updated": updated,
            "unread": len(problems), "errors": errors[:10],
        })
        message = f"Added {added} new and updated {updated} existing lead(s) in {campaign.name}."
        if errors:
            message += f" {len(errors)} failed, first: {errors[0][:160]}"
        return message + _summary(problems)
