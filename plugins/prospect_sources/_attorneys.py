"""
Shared by the attorney prospect sources (courtlistener_attorneys, pacer_attorneys):
reading a docket's free-text attorney block, and tallying attorneys across the
civil cases they appear on. The registry skips this file (leading underscore).
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

from core.models import Prospect

_CITY_STATE_ZIP = re.compile(r"^\s*(?P<city>[A-Za-z .'-]+),?\s+(?P<state>[A-Z]{2})\s+(?P<zip>\d{5})(?:-\d{4})?\s*$")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
PHONE_RE = re.compile(r"\(?\b(\d{3})\)?[\s.-]?(\d{3})[\s.-](\d{4})\b")
_LABELED = re.compile(r"^(email|e-mail|fax|phone|tel|telephone)\s*:", re.I)


def parse_contact(raw: str, name: str = "") -> dict:
    """Firm, street, city/state/zip, phone and email out of a docket's attorney
    block (which sometimes repeats the attorney's own name first)."""
    lines = [l.strip() for l in (raw or "").splitlines()
             if l.strip() and l.strip().lower() != name.strip().lower()]
    found: dict = {}
    for i, line in enumerate(lines):
        m = _CITY_STATE_ZIP.match(line)
        if m:
            found.update(city=m["city"].strip().title(), state=m["state"], zip=m["zip"])
            body = [l for l in lines[:i] if not EMAIL_RE.search(l) and not _LABELED.match(l)]
            if len(body) > 1:
                found["firm"], found["address"] = body[0], ", ".join(body[1:])
            elif body:
                found["address"] = body[0]
            rest = "\n".join(lines[i + 1:])
            phone = next((p for p in PHONE_RE.finditer(rest)
                          if not re.search(r"fax[^\n]*$", rest[:p.start()].rsplit("\n", 1)[-1], re.I)), None)
            if phone:
                found["phone"] = f"({phone[1]}) {phone[2]}-{phone[3]}"
            break
    email = EMAIL_RE.search(raw or "")
    if email:
        found["email"] = email[0].rstrip(".").lower()
    return found


@dataclass
class Tally:
    """One attorney's appearances across the matching cases."""
    name: str
    contact_raw: str = ""
    cases: set = field(default_factory=set)
    suit_natures: Counter = field(default_factory=Counter)
    last_filed: str = ""

    def add(self, case_ref: str, suit_nature: str, filed: str) -> None:
        if case_ref not in self.cases:
            self.cases.add(case_ref)
            self.suit_natures[suit_nature or "Unknown"] += 1
            self.last_filed = max(self.last_filed, filed or "")


def attorney_prospect(source_key: str, external_ref: str, source_url: str, name: str, contact_raw: str,
                      state: str, *, email: Optional[str] = None, phone: Optional[str] = None,
                      tally: Optional[Tally] = None) -> Optional[Prospect]:
    """A civil-litigation attorney Prospect, or None when outside `state` (or no address to tell)."""
    name = " ".join(str(name or "").split())
    if not name:
        return None
    where = parse_contact(contact_raw, name)
    if state and where.get("state") != state:
        return None  # out of state (e.g. admitted pro hac vice), or no address on the docket
    metadata = {
        "external_ref": external_ref,
        "firm": where.get("firm"),
        "contact": {"name": name, "title": "Attorney", "email": email or where.get("email"),
                    "phone": phone or where.get("phone")},
    }
    if tally:
        metadata.update(civil_cases=len(tally.cases), last_filed=tally.last_filed,
                        suit_natures=[n for n, _ in tally.suit_natures.most_common(5)])
    return Prospect(name=name, source=source_key, source_url=source_url, address=where.get("address"),
                    city=where.get("city"), state=where.get("state"), zip=where.get("zip"),
                    focus_area="civil_litigation", metadata=metadata)
