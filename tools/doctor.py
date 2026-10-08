"""Environment doctor: report what's configured vs missing, plugin by plugin.

    python -m tools.doctor          # human-readable report
    python -m tools.doctor --json   # machine-readable

Exit code 0 if the core engine can run (db reachable), 1 otherwise.
Never prints secret values — only whether each is set.
"""

from __future__ import annotations

import json
import os
import sys

_CHECKS = [
    # (env var(s), what it powers, category)
    ("DATABASE_URL", "PostgreSQL database", "core"),
    ("AGENCY_OS_BASE_URL", "Invite links, OAuth metadata", "core"),
    ("SMTP_HOST", "SMTP email channel + welcome emails", "email"),
    ("SMTP_USER", "SMTP email channel", "email"),
    ("SMTP_PASS", "SMTP email channel", "email"),
    ("SMTP_FROM", "SMTP email channel", "email"),
    ("SMARTLEAD_API_KEY", "email_smartlead channel", "email"),
    ("TWILIO_ACCOUNT_SID", "sms_twilio channel", "sms"),
    ("TWILIO_AUTH_TOKEN", "sms_twilio channel", "sms"),
    ("TWILIO_FROM_NUMBER", "sms_twilio channel (or TWILIO_MESSAGING_SERVICE_SID)", "sms"),
    ("APOLLO_API_KEY", "apollo enricher (optional)", "enrich"),
    ("HUNTER_API_KEY", "hunter enricher (optional)", "enrich"),
    ("FIRECRAWL_API_KEY", "firecrawl enricher (optional)", "enrich"),
    ("CALENDLY_SCHEDULING_URL", "calendly booking links", "scheduler"),
    ("CALENDLY_API_TOKEN", "calendly booking sync", "scheduler"),
    ("ANTHROPIC_API_KEY", "Claude AI drafting", "ai"),
    ("AGENCY_OS_LLM", "openai_compatible local model", "ai"),
    ("AGENCY_OS_X402", "buying lead packages", "x402"),
    ("AGENCY_OS_LEAD_PROVIDERS", "allowlisted x402 providers", "x402"),
    ("CDP_API_KEY_ID", "CDP wallet (x402 buying)", "x402"),
    ("AGENCY_OS_SELL", "selling lead packages", "x402"),
    ("AGENCY_OS_SELL_PAY_TO", "payout wallet address", "x402"),
    ("LOB_WEBHOOK_SECRET", "lob return-to-sender webhook", "webhooks"),
    ("AGENCY_OS_WEBHOOK_KEY", "smartlead/generic bounce webhooks", "webhooks"),
]


def report() -> dict:
    by_cat: dict[str, list[dict]] = {}
    for var, purpose, cat in _CHECKS:
        by_cat.setdefault(cat, []).append(
            {"var": var, "purpose": purpose, "set": bool(os.environ.get(var))}
        )
    db_ok = False
    db_err = ""
    try:
        from tools.pipeline import setup  # late: needs env loaded

        _registry, db, _campaigns = setup()
        db.conn.execute("SELECT 1")
        db_ok = True
    except Exception as exc:  # noqa: BLE001 — doctor reports, never raises
        db_err = f"{type(exc).__name__}: {exc}"
    return {"database": {"ok": db_ok, "error": db_err}, "env": by_cat}


def main(argv: list[str]) -> int:
    data = report()
    if "--json" in argv:
        print(json.dumps(data, indent=2))
    else:
        print("agency-os doctor")
        print("================")
        print(f"database: {'OK' if data['database']['ok'] else 'FAIL — ' + data['database']['error']}")
        for cat, checks in data["env"].items():
            print(f"\n[{cat}]")
            for c in checks:
                mark = "✓" if c["set"] else "—"
                print(f"  {mark} {c['var']:32s} {c['purpose']}")
        print("\n(— = not set; many are optional. engine needs only the database.)")
    return 0 if data["database"]["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
