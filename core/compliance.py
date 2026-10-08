"""
CAN-SPAM for outreach email: every commercial email carries a working
unsubscribe link and the sender's postal address, and an address that opted
out is never emailed again.

- The footer is added by the pipeline (core/pipeline.py) and the test send
  (core/cli.py) for email channels only. Welcome and password-reset emails are
  transactional and go out without it.
- The link is /unsubscribe?e=<email>&t=<token>, where the token is an HMAC of
  the lowercased email with AGENCY_OS_UNSUBSCRIBE_SECRET, so it can't be forged
  to unsubscribe someone else and needs no lookup table. The SMTP channel also
  sends it as a one-click List-Unsubscribe header (RFC 8058).
- Opt-outs land in email_suppressions, which every email send checks.

Settings:
  AGENCY_OS_POSTAL_ADDRESS       the sender's physical postal address (a PO box
                                 or commercial mailbox is fine), one line
  AGENCY_OS_UNSUBSCRIBE_SECRET   any long random string; changing it breaks
                                 links in emails already sent
  AGENCY_OS_BASE_URL             the dashboard's public URL (core/welcome.py)

Until all three are set, email channels don't send (problem() says why).
"""

from __future__ import annotations

import hashlib
import hmac
import os
from typing import Optional
from urllib.parse import urlencode

from core.welcome import base_url


def postal_address() -> str:
    return os.environ.get("AGENCY_OS_POSTAL_ADDRESS", "").strip()


def _secret() -> str:
    return os.environ.get("AGENCY_OS_UNSUBSCRIBE_SECRET", "")


def problem() -> Optional[str]:
    """Why outreach email can't be sent compliantly, or None when it can."""
    missing = [name for name, value in (
        ("AGENCY_OS_POSTAL_ADDRESS", postal_address()),
        ("AGENCY_OS_UNSUBSCRIBE_SECRET", _secret()),
        ("AGENCY_OS_BASE_URL", base_url()),
    ) if not value]
    if missing:
        return "CAN-SPAM settings missing: " + ", ".join(missing)
    return None


def normalize(email: str) -> str:
    return (email or "").strip().lower()


def token(email: str) -> str:
    return hmac.new(_secret().encode(), normalize(email).encode(), hashlib.sha256).hexdigest()[:32]


def token_ok(email: str, given: str) -> bool:
    return bool(_secret() and normalize(email) and given) and hmac.compare_digest(token(email), given)


def unsubscribe_url(email: str) -> str:
    return f"{base_url()}/unsubscribe?" + urlencode({"e": normalize(email), "t": token(email)})


def footer(email: str) -> str:
    return (
        "\n\n--\n"
        f"{postal_address()}\n"
        f"Don't want these emails? Unsubscribe: {unsubscribe_url(email)}\n"
    )


def with_footer(body: str, email: str) -> str:
    return body.rstrip() + footer(email)
