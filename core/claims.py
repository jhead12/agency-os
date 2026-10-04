"""
Guarantee claims: when failed leads make a package's 90% impossible, ask the
provider for a remedy, with the evidence for each failed lead.

Provider contract:

    POST {provider}/packages/{id}/claims
      {"unlock_tx", "lead_count", "failed_count", "shortfall", "verified_rate",
       "expected_refund_atomic", "failed": [{"lead_id", "check", "evidence": [...], "ai_reason"}]}
    -> {"remedy": "replacement", "leads": [...]}
     | {"remedy": "refund", "refund_tx": "0x...", "refund_atomic": "..."}
     | {"remedy": "disputed", "reason": "..."}

Replacement leads are imported into the same campaign and package, and are
extras: the guarantee stays measured on the leads originally sold. A refund
is recorded as a pending `refund_in` in the spend ledger until someone
confirms it onchain (`spend resolve`). One claim can be in flight per
package, claims are only accepted inside the verification window, and each
failed lead is claimed once.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

import httpx

from core import lead_packages, verify
from core.access import CurrentUser
from core.db import Database
from core.lead_packages import _json_body, _package_url, add_leads, configured_providers, parse_lead
from core.payments import USDC

_TX_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
REMEDIED = ("filed", "replaced", "refunded")


@dataclass
class ClaimResult:
    ok: bool
    message: str
    claim_id: Optional[int] = None


def package_status(db: Database, lead_package_id: int, *, reviewer=None) -> Optional[dict]:
    """Everything the package page and a claim need: verdicts, counts, window, claims."""
    lp = db.get_lead_package(lead_package_id)
    if lp is None:
        return None
    checks = verify.evaluate_package(db, lead_package_id, reviewer=reviewer)
    for check in checks:
        check["signals"] = verify.as_json(check["signals"])
    rules = verify.stored_rules(lp)
    window = verify.window_state(db, lp, rules)
    summary = verify.summarize(lp, checks, window, rules)
    claims = [dict(r) for r in db.conn.execute(
        "SELECT * FROM package_claims WHERE lead_package_id = ? ORDER BY id DESC", (lead_package_id,)).fetchall()]
    remedied = sum(c["shortfall"] for c in claims if c["status"] in REMEDIED)
    summary["claimable"] = max(0, summary["shortfall"] - remedied)
    in_flight = any(c["status"] == "filed" for c in claims)
    problem = None
    if not window["claims_open"]:
        problem = "The verification window and the claim period have closed"
    elif in_flight:
        problem = "A claim is already in progress"
    elif summary["shortfall"] and (not summary["claimable"] or not summary["unclaimed_failed"]):
        problem = "Earlier claims already cover the shortfall"
    elif not summary["shortfall"]:
        problem = "No shortfall to claim: the package is meeting its guarantee"
    elif lp["provider"] not in configured_providers():
        problem = "The provider is no longer on the allowlist"
    return {"lp": lp, "checks": checks, "summary": summary, "claims": claims, "rules": rules,
            "window": window, "days_left": window["days_left"], "claim_problem": problem}


def expected_refund(db: Database, lp: dict, failed: list[dict], shortfall: int, lead_count: int) -> int:
    """Pro-rata unlock refund plus the royalties we paid on the failed leads."""
    unlock_share = lp["unlock_price_atomic"] * shortfall // lead_count if lead_count else 0
    ids = [r["prospect_id"] for r in failed]
    royalties = db.conn.execute(
        """SELECT COALESCE(SUM(amount_atomic), 0) AS total FROM spend
           WHERE kind = 'royalty' AND status = 'settled' AND lead_package_id = ? AND prospect_id = ANY(?)""",
        (lp["id"], ids),
    ).fetchone()["total"] if ids else 0
    return unlock_share + royalties


def _claim_body(lp: dict, summary: dict, failed: list[dict], expected: int, rules) -> dict:
    return {
        "unlock_tx": lp["tx_hash"] or "",
        "lead_count": summary["lead_count"],
        "failed_count": summary["failed"],
        "shortfall": summary["claimable"],
        "verified_rate": round(summary["rate"], 4) if summary["rate"] is not None else None,
        "expected_refund_atomic": str(expected),
        "rules": rules.to_dict(),
        "failed": [{
            "lead_id": r["lead_id"],
            "check": next((s["check"] for s in sorted(verify.as_json(r["signals"]), key=lambda s: -s["weight"])), ""),
            "evidence": [s["label"] for s in verify.as_json(r["signals"]) if s["weight"] > 0],
            "ai_reason": r["ai_reason"] or "",
        } for r in failed],
    }


def _finish(db: Database, claim_id: int, status: str, user: Optional[CurrentUser], *,
            release_leads: bool = False, **fields) -> None:
    """Close a claim. release_leads lets its leads be claimed again (after an error)."""
    c = db.conn
    sets = ", ".join(f"{k} = ?" for k in fields)
    with c.raw.transaction():
        c.execute(f"UPDATE package_claims SET status = ?, resolved_at = CURRENT_TIMESTAMP"
                  f"{', ' + sets if sets else ''} WHERE id = ?", (status, *fields.values(), claim_id))
        if release_leads:
            c.execute("UPDATE lead_checks SET claim_id = NULL WHERE claim_id = ?", (claim_id,))
        db._audit(c, user, f"claim.{status}", "package_claim", claim_id, {k: v for k, v in fields.items()})


def file_claim(db: Database, lead_package_id: int, user: Optional[CurrentUser], *,
               http: Optional[httpx.Client] = None) -> ClaimResult:
    """File a guarantee claim with the provider and apply the remedy. Never raises."""
    status = package_status(db, lead_package_id)
    if status is None:
        return ClaimResult(False, "Package not found")
    if status["claim_problem"]:
        return ClaimResult(False, status["claim_problem"])
    lp, summary = status["lp"], status["summary"]
    failed = summary["unclaimed_failed"]
    expected = expected_refund(db, lp, failed, summary["claimable"], summary["lead_count"])

    # Record the claim and mark its leads first, under a lock on the package,
    # so two people pressing "File claim" can't claim the same leads twice.
    c = db.conn
    with c.raw.transaction():
        c.execute("SELECT id FROM lead_packages WHERE id = ? FOR UPDATE", (lp["id"],))
        if c.execute("SELECT 1 FROM package_claims WHERE lead_package_id = ? AND status = 'filed'",
                     (lp["id"],)).fetchone():
            return ClaimResult(False, "A claim is already in progress")
        claim_id = c.execute(
            """INSERT INTO package_claims (lead_package_id, status, lead_count, failed_count, shortfall,
                   verified_rate, expected_refund_atomic, filed_by)
               VALUES (?, 'filed', ?, ?, ?, ?, ?, ?) RETURNING id""",
            (lp["id"], summary["lead_count"], summary["failed"], summary["claimable"], summary["rate"],
             expected, user.id if user else None),
        ).fetchone()["id"]
        c.execute("UPDATE lead_checks SET claim_id = ? WHERE id = ANY(?) AND claim_id IS NULL",
                  (claim_id, [r["id"] for r in failed]))
        db._audit(c, user, "claim.filed", "package_claim", claim_id,
                  {"lead_package_id": lp["id"], "shortfall": summary["claimable"], "failed": len(failed)})

    client = http or lead_packages.make_http()
    try:
        response = client.post(_package_url(lp["provider"], lp["package_id"], "claims"),
                               json=_claim_body(lp, summary, failed, expected, status["rules"]))
        data = _json_body(response) if response.is_success else None
    except (httpx.HTTPError, ValueError) as exc:
        _finish(db, claim_id, "error", user, release_leads=True, reason=f"Provider unreachable: {exc}"[:500])
        return ClaimResult(False, f"Claim not delivered: {exc}", claim_id)
    finally:
        if http is None:
            client.close()
    if not isinstance(data, dict):
        reason = f"Provider returned {response.status_code}"
        _finish(db, claim_id, "error", user, release_leads=True, reason=reason)
        return ClaimResult(False, f"Claim not accepted: {reason}", claim_id)
    return _apply_remedy(db, lp, claim_id, summary["claimable"], data, user)


def _apply_remedy(db: Database, lp: dict, claim_id: int, shortfall: int, data: dict,
                  user: Optional[CurrentUser]) -> ClaimResult:
    remedy = data.get("remedy")
    if remedy == "replacement":
        raw = data.get("leads") if isinstance(data.get("leads"), list) else []
        leads = [l for l in (parse_lead(i) for i in raw[:shortfall]) if l]
        if not leads:
            _finish(db, claim_id, "error", user, release_leads=True, reason="Replacement had no usable leads")
            return ClaimResult(False, "The provider offered replacements, but none were usable", claim_id)
        c = db.conn
        with c.raw.transaction():
            imported, duplicates = add_leads(db, lp, lp["campaign_id"], leads, replacement_for_claim=claim_id)
        _finish(db, claim_id, "replaced", user, remedy="replacement", replacement_count=imported)
        note = f", {duplicates} already in agency-os" if duplicates else ""
        short = f" ({shortfall - len(leads)} short of the shortfall)" if len(leads) < shortfall else ""
        return ClaimResult(True, f"Claim settled with {imported} replacement leads{note}{short}", claim_id)

    if remedy == "refund":
        tx = str(data.get("refund_tx") or "")
        try:
            amount = int(str(data.get("refund_atomic")))
        except (TypeError, ValueError):
            amount = 0
        if not _TX_RE.match(tx) or amount <= 0:
            _finish(db, claim_id, "error", user, release_leads=True, reason="Refund response was malformed")
            return ClaimResult(False, "The provider's refund response was malformed", claim_id)
        c = db.conn
        with c.raw.transaction():
            c.execute(
                """INSERT INTO spend (kind, ref, campaign_id, user_id, lead_package_id, amount_atomic,
                       asset, network, tx_hash, status, error)
                   VALUES ('refund_in', ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
                (f"claim:{claim_id}", lp["campaign_id"], user.id if user else None, lp["id"], amount,
                 USDC.get(lp["network"]), lp["network"], tx, "Refund reported by the provider; confirm onchain"),
            )
        _finish(db, claim_id, "refunded", user, remedy="refund", refund_atomic=amount, refund_tx=tx)
        return ClaimResult(True, "Refund reported. Confirm the transaction onchain, then mark it settled "
                                 "with `spend resolve`.", claim_id)

    if remedy == "disputed":
        reason = str(data.get("reason") or "No reason given")[:500]
        _finish(db, claim_id, "disputed", user, remedy="disputed", reason=reason)
        return ClaimResult(False, f"The provider disputed the claim: {reason}", claim_id)

    _finish(db, claim_id, "error", user, release_leads=True, reason=f"Unknown remedy: {str(remedy)[:50]}")
    return ClaimResult(False, "The provider's response named no remedy", claim_id)


def ratings(db: Database) -> dict:
    """Measured verified rate per package and per provider, across every unlock here.

    Keys are (provider, package_id) and provider. Only leads sold count,
    not replacements; a rate needs at least one worked lead.
    """
    rows = db.conn.execute(
        """SELECT lp.provider, lp.package_id, COUNT(DISTINCT lp.id) AS unlocks,
                  COUNT(*) FILTER (WHERE lc.status = 'verified') AS verified,
                  COUNT(*) FILTER (WHERE lc.status = 'failed') AS failed
           FROM lead_checks lc JOIN lead_packages lp ON lp.id = lc.lead_package_id
           WHERE lc.replacement = 0 GROUP BY lp.provider, lp.package_id""",
    ).fetchall()
    out: dict = {}
    for r in rows:
        for key in ((r["provider"], r["package_id"]), r["provider"]):
            agg = out.setdefault(key, {"verified": 0, "failed": 0, "unlocks": 0})
            agg["verified"] += r["verified"]
            agg["failed"] += r["failed"]
            agg["unlocks"] += r["unlocks"]
    for agg in out.values():
        worked = agg["verified"] + agg["failed"]
        agg["rate"] = agg["verified"] / worked if worked else None
    return out

