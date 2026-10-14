"""
Lead list import: CSV text → prospects in a campaign.

Used by the campaign page's CSV upload (web/app.py) and the import_leads tool
(core/tools.py), so a list added by hand and one added by an AI assistant
land the same way.

Prospects are upserted (matched by EIN or name+state). A prospect new to the
campaign gets an outreach row at the 'cold' stage, and contact columns are
written to that row.
"""

from __future__ import annotations

import csv
import io

from core.models import Prospect

# Maps CSV column headers (case-insensitive) to Prospect model fields.
# Multiple header variants are accepted for each field.
CSV_FIELD_MAP = {
    "name": ["name", "organization", "organization name", "org", "company"],
    "ein": ["ein", "tax id", "tax_id"],
    "ntee_code": ["ntee", "ntee_code", "ntee code", "category"],
    "website_url": ["website", "website_url", "url", "domain"],
    "address": ["address", "street", "address1", "address_1", "street_address"],
    "city": ["city"],
    "state": ["state", "st"],
    "zip": ["zip", "zip_code", "zipcode", "postal", "postal_code"],
    "county": ["county"],
    "focus_area": ["focus_area", "focus area", "focus", "category"],
    "annual_revenue": ["annual_revenue", "annual revenue", "revenue", "income"],
    "voter_engagement": ["voter_engagement", "voter engagement"],
    "source": ["source", "lead_source", "lead source"],
    "source_url": ["source_url", "source url", "referral", "ref"],
    # Outreach-level contact info (goes on the outreach row, not prospect)
    "contact_name": ["contact_name", "contact", "contact name", "first name", "firstname"],
    "contact_email": ["contact_email", "email", "contact email", "e-mail"],
    "contact_phone": ["contact_phone", "phone", "contact phone", "telephone", "tel"],
    "contact_title": ["contact_title", "contact title", "title", "position"],
}

TEMPLATE_HEADERS = [
    "name", "ein", "ntee_code", "website_url", "address",
    "city", "state", "zip", "county", "focus_area",
    "annual_revenue", "voter_engagement", "source",
    "contact_name", "contact_email", "contact_phone", "contact_title",
]


class LeadImportError(ValueError):
    """The list can't be imported at all; the message is safe to show."""


def parse_headers(headers: list[str]) -> dict[str, int]:
    """Map CSV column headers to field names. Returns {field: col_index}."""
    mapping = {}
    normalized = [h.strip().lower() for h in headers]
    for field, variants in CSV_FIELD_MAP.items():
        for i, col in enumerate(normalized):
            if col in variants and field not in mapping:
                mapping[field] = i
    return mapping


def import_csv(db, campaign, text: str, *, max_rows: int | None = None) -> dict:
    """Add the leads in `text` to `campaign`. Returns {imported, updated, skipped, errors}."""
    rows = list(csv.reader(io.StringIO(text)))
    if len(rows) < 2:
        raise LeadImportError("CSV needs a header row and at least one data row.")
    if max_rows is not None and len(rows) - 1 > max_rows:
        raise LeadImportError(f"That list has {len(rows) - 1} rows; import at most {max_rows} at a time.")

    field_map = parse_headers(rows[0])
    if "name" not in field_map:
        raise LeadImportError("CSV must have a 'name' or 'organization' column.")

    campaign_id = db.get_campaign_id(campaign.db_name) or db.upsert_campaign(campaign.db_name, str(campaign.config_dir))

    imported = updated = skipped = 0
    errors = []

    for row_idx, row in enumerate(rows[1:], start=2):
        if not row or all(not c.strip() for c in row):
            skipped += 1
            continue

        def get(field):
            idx = field_map.get(field)
            if idx is None or idx >= len(row):
                return ""
            return row[idx].strip()

        org_name = get("name")
        if not org_name:
            skipped += 1
            continue

        rev_str = get("annual_revenue").replace(",", "").replace("$", "")
        try:
            annual_revenue = int(rev_str) if rev_str else None
        except ValueError:
            annual_revenue = None

        prospect = Prospect(
            name=org_name,
            ein=get("ein") or None,
            ntee_code=get("ntee_code") or None,
            website_url=get("website_url") or None,
            address=get("address") or None,
            city=get("city") or None,
            state=get("state") or None,
            zip=get("zip") or None,
            county=get("county") or None,
            focus_area=get("focus_area") or None,
            annual_revenue=annual_revenue,
            voter_engagement=get("voter_engagement").lower() in ("1", "true", "yes", "y"),
            source=get("source") or "csv_upload",
            source_url=get("source_url") or None,
            metadata={},
        )

        try:
            prospect_id = db.upsert_prospect(prospect)
            existing = db.conn.execute(
                "SELECT created_at, updated_at FROM prospects WHERE id = ?",
                (prospect_id,),
            ).fetchone()
            is_new = existing and existing["created_at"] == existing["updated_at"]

            outreach_id = db.upsert_outreach(prospect_id, campaign_id)

            contact = {k: get(k) for k in ("contact_name", "contact_email", "contact_phone", "contact_title")}
            if contact["contact_name"] or contact["contact_email"] or contact["contact_phone"]:
                db.update_outreach(outreach_id, contact)

            if is_new:
                imported += 1
            else:
                updated += 1
        except Exception as exc:
            errors.append(f"Row {row_idx}: {exc}")
            skipped += 1

    return {"imported": imported, "updated": updated, "skipped": skipped, "errors": errors}
