"""
Selling our own lists as x402 lead packages (the provider side of
core/lead_packages.py, so another agency-os, or this one, can buy from us).

    GET  /x402/packages                     free catalog
    GET  /x402/packages/{slug}/leads        x402: the unlock; returns the leads and a claim token
    POST /x402/packages/{slug}/contacts     x402: the royalty when a buyer first contacts a lead
    POST /x402/packages/{slug}/claims       a guarantee claim, authorized by the claim token

Off unless AGENCY_OS_SELL=on with AGENCY_OS_SELL_PAY_TO (our wallet address).
Payments are verified and settled through an x402 facilitator
(AGENCY_OS_X402_FACILITATOR; x402.org's free facilitator handles Base Sepolia).

What we sell, and the rules:

- A package is published from a saved prospect list. Only leads we can stand
  behind go in: a contact on file, no bounced email, and our own contact
  history (calls, mail, email; the evidence a buyer can check) at or above
  the promised tier. Leads marked do-not-sell, and leads we bought from
  someone else, are never sold.
- Leads are re-checked at each sale, so one that went bad since publishing
  isn't sold.
- A royalty is only charged for a lead sold to that buyer, once per lead.
- A claim needs the sale's claim token (an unlock's transaction hash is
  public, so it can't authorize anything). Claimed leads we've reached
  ourselves since the sale are rejected; the rest are capped at what
  actually breaks 90%. Replacements come from the same list; anything we
  can't replace goes to an owner to review and refund by hand. Refunds are
  never sent automatically.
"""

from __future__ import annotations

import base64
import json
import os
import re
import secrets
from dataclasses import dataclass
from typing import Any, Optional, Protocol

from core import contact_depth, royalties, verify
from core.access import hash_token
from core.payments import (
    MAINNETS, NETWORKS, PAYMENT_RESPONSE_HEADER, PAYMENT_REQUIRED_HEADER, USDC, mainnet_allowed, usd_to_atomic,
)

MAX_PACKAGE_LEADS = 5000
MAX_CLAIM_BODY_LEADS = 5000
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
DEFAULT_FACILITATOR = "https://x402.org/facilitator"


# ── Configuration ──────────────────────────────────────────────────────


def network_key() -> str:
    key = os.environ.get("AGENCY_OS_SELL_NETWORK", "base-sepolia").strip()
    return key if key in NETWORKS else "base-sepolia"


def caip() -> str:
    return NETWORKS[network_key()]


def pay_to() -> str:
    address = os.environ.get("AGENCY_OS_SELL_PAY_TO", "").strip()
    return address if _ADDRESS_RE.match(address) else ""


def problem() -> Optional[str]:
    """Why we can't sell right now, or None."""
    if os.environ.get("AGENCY_OS_SELL", "").strip().lower() not in ("1", "on", "true", "yes"):
        return "Selling is off (AGENCY_OS_SELL)"
    if not pay_to():
        return "No payout address (AGENCY_OS_SELL_PAY_TO)"
    if network_key() in MAINNETS and not mainnet_allowed():
        return "Mainnet sales need AGENCY_OS_X402_ALLOW_MAINNET=1"
    return None


# ── The payment gate (x402 facilitator) ────────────────────────────────


@dataclass
class Verified:
    ok: bool
    payer: str = ""
    error: str = ""
    context: Any = None


@dataclass
class Settled:
    ok: bool
    tx_hash: str = ""
    header: str = ""
    error: str = ""


class Gate(Protocol):
    def payment_required(self, amount_atomic: int, url: str, description: str) -> str:
        """The PAYMENT-REQUIRED header value for this price."""

    def verify(self, header: str, amount_atomic: int, url: str) -> Verified: ...

    def settle(self, verified: Verified) -> Settled: ...


class X402Gate:
    """Verifies and settles through an x402 facilitator with the x402 SDK (requirements-payments.txt)."""

    def __init__(self, facilitator: Any = None):
        self._server = None
        self._facilitator = facilitator  # tests pass a stand-in; otherwise the HTTP facilitator

    def _resource_server(self):
        if self._server is None:
            from x402 import x402ResourceServerSync
            from x402.http import HTTPFacilitatorClientSync
            from x402.http.facilitator_client_base import FacilitatorConfig
            from x402.mechanisms.evm.exact import ExactEvmServerScheme

            url = os.environ.get("AGENCY_OS_X402_FACILITATOR", DEFAULT_FACILITATOR)
            facilitator = self._facilitator or HTTPFacilitatorClientSync(FacilitatorConfig(url=url))
            server = x402ResourceServerSync(facilitator)
            server.register(caip(), ExactEvmServerScheme())
            server.initialize()
            self._server = server
        return self._server

    def _requirements(self, amount_atomic: int):
        from x402.schemas import AssetAmount, ResourceConfig

        return self._resource_server().build_payment_requirements(ResourceConfig(
            scheme="exact", pay_to=pay_to(), network=caip(), max_timeout_seconds=300,
            price=AssetAmount(amount=str(amount_atomic), asset=USDC[caip()])))

    def payment_required(self, amount_atomic: int, url: str, description: str) -> str:
        from x402.http.utils import encode_payment_required_header
        from x402.schemas import ResourceInfo

        required = self._resource_server().create_payment_required_response(
            self._requirements(amount_atomic), resource=ResourceInfo(url=url, description=description[:200]))
        return encode_payment_required_header(required)

    def verify(self, header: str, amount_atomic: int, url: str) -> Verified:
        from x402.http.utils import decode_payment_signature_header

        try:
            payload = decode_payment_signature_header(header)
            server = self._resource_server()
            requirement = server.find_matching_requirements(self._requirements(amount_atomic), payload)
            if requirement is None:
                return Verified(False, error="Payment doesn't match the price")
            result = server.verify_payment(payload, requirement)
        except Exception as exc:  # malformed headers and facilitator outages are the buyer's error, not a crash
            return Verified(False, error=f"Payment not verified: {type(exc).__name__}")
        if not result.is_valid:
            return Verified(False, error=f"Payment not valid: {result.invalid_reason or 'rejected'}")
        return Verified(True, payer=(result.verify.payer or "").lower(), context=(payload, requirement))

    def settle(self, verified: Verified) -> Settled:
        from x402.http.utils import encode_payment_response_header

        try:
            payload, requirement = verified.context
            response = self._resource_server().settle_payment(payload, requirement)
        except Exception as exc:
            return Settled(False, error=f"Settlement failed: {type(exc).__name__}")
        if not response.success or not response.transaction:
            return Settled(False, error=response.error_reason or "Settlement failed")
        return Settled(True, tx_hash=response.transaction, header=encode_payment_response_header(response))


_gate: Optional[Gate] = None


def default_gate() -> Gate:
    global _gate
    if _gate is None:
        _gate = X402Gate()
    return _gate


# ── Choosing leads ─────────────────────────────────────────────────────


def _slug(title: str) -> str:
    base = _SLUG_RE.sub("-", title.lower()).strip("-")[:40] or "package"
    return f"{base}-{secrets.token_hex(3)}"


def _check_lead(db, prospect_id: int, promised: str) -> tuple[bool, str, dict]:
    """(eligible, our tier, contact) for one prospect, from evidence a buyer can verify."""
    history, _ = contact_depth.history_for_prospect(db, prospect_id)
    tier = contact_depth.tier_of(history)  # no stage: buyers can only check what's in the history
    contact = db.conn.execute(
        """SELECT contact_name, contact_email, contact_phone, contact_title FROM outreach
           WHERE prospect_id = ? AND (contact_email IS NOT NULL OR contact_phone IS NOT NULL)
           ORDER BY updated_at DESC LIMIT 1""", (prospect_id,)).fetchone()
    if contact is None or contact_depth.RANK[tier] < contact_depth.RANK.get(promised, 0):
        return False, tier, {}
    contact = dict(contact)
    if contact["contact_email"] and contact["contact_email"].lower() in verify.bounced_emails(db, prospect_id):
        return False, tier, {}
    return True, tier, contact


def candidates(db, criteria: dict, exclude: set[int] = frozenset(), limit: int = MAX_PACKAGE_LEADS) -> list[int]:
    """Prospects a list's filters match that we may sell (not do-not-sell, not bought from others)."""
    where, params = db.prospect_filter(criteria)
    rows = db.conn.execute(
        f"""SELECT DISTINCT p.id FROM prospects p LEFT JOIN outreach o ON p.id = o.prospect_id
            WHERE {where} AND p.do_not_sell = 0 AND COALESCE(p.source, '') NOT LIKE 'x402:%'
            ORDER BY p.id LIMIT ?""", (*params, limit + len(exclude))).fetchall()
    return [r["id"] for r in rows if r["id"] not in exclude][:limit]


def preview(db, criteria: dict, promised: str) -> dict:
    """How many of a list's prospects would go into a package at this tier."""
    matched = candidates(db, criteria)
    eligible = [(pid, tier) for pid in matched for ok, tier, _ in [_check_lead(db, pid, promised)] if ok]
    mix: dict = {}
    for _, tier in eligible:
        mix[tier] = mix.get(tier, 0) + 1
    return {"matched": len(matched), "eligible": eligible, "tier_mix": mix,
            "rate": len(eligible) / len(matched) if matched else None}


# ── Publishing ─────────────────────────────────────────────────────────


def publish(db, user, *, saved_list_id: int, title: str, industry: str, region: str, unlock_usd: Any,
            royalty_usd: dict, guarantee_tier: str, rules: Optional[dict], consent_note: str,
            sms_consent: bool, contributor_pool_pct: Any = 20) -> tuple[Optional[int], str]:
    """Publish a saved list as a package. Returns (package id, "") or (None, why not)."""
    saved = db.get_prospect_saved_list(saved_list_id)
    if saved is None:
        return None, "Saved list not found"
    if guarantee_tier not in contact_depth.RANK or guarantee_tier == "unworked":
        return None, "Pick the contact depth the package guarantees"
    title = (title or "").strip()[:200]
    if not title:
        return None, "Give the package a title"
    unlock = usd_to_atomic(unlock_usd)
    prices = {tier: usd_to_atomic(v) for tier, v in (royalty_usd or {}).items()
              if tier in contact_depth.RANK and tier != "unworked" and str(v).strip()}
    if unlock <= 0:
        return None, "Set an unlock price"
    try:
        pool = max(0, min(100, int(contributor_pool_pct)))
    except (TypeError, ValueError):
        pool = 20
    found = preview(db, saved["criteria"], guarantee_tier)
    if not found["eligible"]:
        return None, "None of this list's prospects meet that contact depth yet"
    checked_rules = verify.DEFAULT_RULES.merged(rules or {}).to_dict()
    c = db.conn
    with c.raw.transaction():
        package_id = c.execute(
            """INSERT INTO published_packages (slug, saved_list_id, criteria, title, industry, region, consent_note,
                   sms_consent, guarantee_tier, rules, unlock_price_atomic, royalty_atomic, royalty_by_tier,
                   contributor_pool_pct, created_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id""",
            (_slug(title), saved_list_id, json.dumps(saved["criteria"]), title, (industry or "")[:100],
             (region or "")[:100], (consent_note or "")[:500], int(sms_consent), guarantee_tier,
             json.dumps(checked_rules), unlock, max(prices.values(), default=0), json.dumps(prices), pool,
             getattr(user, "id", None)),
        ).fetchone()["id"]
        c.executemany(
            "INSERT INTO published_leads (package_id, prospect_id, lead_id, tier) VALUES (?, ?, ?, ?)",
            [(package_id, pid, f"L{secrets.token_hex(8)}", tier) for pid, tier in found["eligible"]])
        db._audit(c, user, "selling.publish", "published_package", package_id,
                  {"title": title, "leads": len(found["eligible"]), "unlock_atomic": unlock})
    return package_id, ""


def set_active(db, user, package_id: int, active: bool) -> None:
    c = db.conn
    with c.raw.transaction():
        c.execute("UPDATE published_packages SET active = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                  (int(active), package_id))
        db._audit(c, user, "selling.active", "published_package", package_id, {"active": active})


def set_do_not_sell(db, user, prospect_id: int, flag: bool) -> None:
    c = db.conn
    with c.raw.transaction():
        c.execute("UPDATE prospects SET do_not_sell = ? WHERE id = ?", (int(flag), prospect_id))
        db._audit(c, user, "prospect.do_not_sell", "prospect", prospect_id, {"do_not_sell": flag})


# ── Catalog and lead payloads ──────────────────────────────────────────


def _package(db, slug: str) -> Optional[dict]:
    row = db.conn.execute("SELECT * FROM published_packages WHERE slug = ? AND active = 1", (slug,)).fetchone()
    return dict(row) if row else None


def _sellable(db, package_id: int, replacement: Optional[bool] = False) -> list[dict]:
    sql = """SELECT pl.* FROM published_leads pl JOIN prospects p ON p.id = pl.prospect_id
             WHERE pl.package_id = ? AND p.do_not_sell = 0"""
    if replacement is not None:
        sql += f" AND pl.replacement = {int(replacement)}"
    return [dict(r) for r in db.conn.execute(sql + " ORDER BY pl.id", (package_id,)).fetchall()]


def catalog(db) -> list[dict]:
    """The free catalog, in the shape core/lead_packages.Package.from_catalog reads."""
    out = []
    for row in db.conn.execute("SELECT * FROM published_packages WHERE active = 1 ORDER BY id").fetchall():
        pkg = dict(row)
        leads = _sellable(db, pkg["id"])
        if not leads:
            continue
        mix: dict = {}
        for lead in leads:
            mix[lead["tier"]] = mix.get(lead["tier"], 0) + 1
        rules = json.loads(pkg["rules"])
        out.append({
            "id": pkg["slug"], "title": pkg["title"], "industry": pkg["industry"], "region": pkg["region"],
            "lead_count": len(leads), "tier_mix": mix, "updated_at": str(pkg["updated_at"])[:10],
            "unlock_price_atomic": str(pkg["unlock_price_atomic"]), "royalty_atomic": str(pkg["royalty_atomic"]),
            "royalty_by_tier": {k: str(v) for k, v in json.loads(pkg["royalty_by_tier"]).items()},
            "pay_to": pay_to(), "network": caip(), "sms_consent": bool(pkg["sms_consent"]),
            "consent_note": pkg["consent_note"], "sourcing": "Researched and contacted by our team",
            "guarantee": {"verified_rate_min": 0.9, "tier": pkg["guarantee_tier"],
                          "window_days": rules["window_days"], "rules": rules},
        })
    return out


def _lead_payload(db, lead: dict) -> Optional[dict]:
    p = db.get_prospect(lead["prospect_id"])
    if p is None:
        return None
    ok, _tier, contact = _check_lead(db, lead["prospect_id"], "unworked")
    history, _ = contact_depth.history_for_prospect(db, lead["prospect_id"])
    return {
        "lead_id": lead["lead_id"], "name": p.name, "ein": p.ein, "address": p.address, "city": p.city,
        "state": p.state, "zip": p.zip, "county": p.county, "ntee_code": p.ntee_code, "focus_area": p.focus_area,
        "website_url": p.website_url, **(contact if ok else {}), "tier": lead["tier"],
        "contact_history": [{k: h[k] for k in ("channel", "outcome", "at", "decision_maker", "org_confirmed", "pitched")}
                            for h in history][-contact_depth.MAX_HISTORY:],
    }


# ── The paid endpoints ─────────────────────────────────────────────────


@dataclass
class Reply:
    status: int
    body: dict
    headers: dict


def _b64(data: dict) -> str:
    return base64.b64encode(json.dumps(data).encode()).decode()


def _require(gate: Gate, amount: int, url: str, description: str) -> Reply:
    return Reply(402, {"error": "payment required"}, {PAYMENT_REQUIRED_HEADER: gate.payment_required(amount, url, description)})


def sell_leads(db, gate: Gate, slug: str, payment: str, url: str) -> Reply:
    """The unlock: 402 with the price, or (paid) the leads plus a claim token."""
    pkg = _package(db, slug)
    if pkg is None:
        return Reply(404, {"error": "Package not found"}, {})
    amount = pkg["unlock_price_atomic"]
    if not payment:
        return _require(gate, amount, url, f"Unlock {pkg['title']}")
    checked = gate.verify(payment, amount, url)
    if not checked.ok:
        return Reply(402, {"error": checked.error}, {})
    leads = [lead for lead in _sellable(db, pkg["id"]) if _check_lead(db, lead["prospect_id"], pkg["guarantee_tier"])[0]]
    if not leads:
        return Reply(409, {"error": "No leads are available right now"}, {})  # never charge for nothing
    settled = gate.settle(checked)
    if not settled.ok:
        return Reply(402, {"error": settled.error}, {})
    token = secrets.token_urlsafe(32)
    c = db.conn
    with c.raw.transaction():
        sale_id = c.execute(
            """INSERT INTO package_sales (package_id, payer, network, tx_hash, amount_atomic, claim_token_hash)
               VALUES (?, ?, ?, ?, ?, ?) RETURNING id""",
            (pkg["id"], checked.payer, caip(), settled.tx_hash, amount, hash_token(token))).fetchone()["id"]
        c.executemany("INSERT INTO sale_leads (sale_id, published_lead_id) VALUES (?, ?)",
                      [(sale_id, lead["id"]) for lead in leads])
        # The reps who built each lead share in its part of the unlock (core/royalties.py).
        royalties.accrue(db, c, sale_id, "unlock", {lead["id"]: amount // len(leads) for lead in leads})
        c.execute(
            """INSERT INTO spend (kind, ref, amount_atomic, asset, network, pay_to, tx_hash, status)
               VALUES ('unlock_in', ?, ?, ?, ?, ?, ?, 'settled')""",
            (f"sale:{sale_id}", amount, USDC[caip()], caip(), pay_to(), settled.tx_hash))
        db._audit(c, None, "selling.sale", "published_package", pkg["id"],
                  {"sale_id": sale_id, "payer": checked.payer, "leads": len(leads), "tx_hash": settled.tx_hash})
    payload = [p for p in (_lead_payload(db, lead) for lead in leads) if p]
    return Reply(200, {"leads": payload, "claim_token": token}, {PAYMENT_RESPONSE_HEADER: settled.header})


def royalty(db, gate: Gate, slug: str, body: Any, payment: str, url: str) -> Reply:
    """The per-lead royalty, charged only for a lead sold to the paying buyer, once."""
    pkg = _package(db, slug)
    lead_id = str((body or {}).get("lead_id") or "") if isinstance(body, dict) else ""
    lead = db.conn.execute("SELECT * FROM published_leads WHERE package_id = ? AND lead_id = ?",
                           (pkg["id"], lead_id)).fetchone() if pkg else None
    if lead is None:
        return Reply(404, {"error": "Lead not found"}, {})
    amount = json.loads(pkg["royalty_by_tier"]).get(lead["tier"], pkg["royalty_atomic"])
    if amount <= 0:
        return Reply(200, {"ok": True, "royalty": 0}, {})
    if not payment:
        return _require(gate, amount, url, f"Royalty for {lead_id}")
    checked = gate.verify(payment, amount, url)
    if not checked.ok:
        return Reply(402, {"error": checked.error}, {})
    sale = db.conn.execute(
        """SELECT s.id FROM package_sales s JOIN sale_leads sl ON sl.sale_id = s.id
           WHERE s.package_id = ? AND s.payer = ? AND sl.published_lead_id = ? ORDER BY s.id DESC LIMIT 1""",
        (pkg["id"], checked.payer, lead["id"])).fetchone()
    if sale is None:
        return Reply(403, {"error": "This lead wasn't sold to this buyer"}, {})
    if db.conn.execute("SELECT 1 FROM sale_royalties WHERE sale_id = ? AND published_lead_id = ?",
                       (sale["id"], lead["id"])).fetchone():
        return Reply(409, {"error": "Royalty already paid for this lead"}, {})
    settled = gate.settle(checked)
    if not settled.ok:
        return Reply(402, {"error": settled.error}, {})
    c = db.conn
    with c.raw.transaction():
        c.execute("INSERT INTO sale_royalties (sale_id, published_lead_id, tx_hash, amount_atomic) VALUES (?, ?, ?, ?)",
                  (sale["id"], lead["id"], settled.tx_hash, amount))
        royalties.accrue(db, c, sale["id"], "royalty", {lead["id"]: amount})
        c.execute(
            """INSERT INTO spend (kind, ref, amount_atomic, asset, network, pay_to, tx_hash, status)
               VALUES ('royalty_in', ?, ?, ?, ?, ?, ?, 'settled')""",
            (f"sale:{sale['id']}/{lead_id}", amount, USDC[caip()], caip(), pay_to(), settled.tx_hash))
    return Reply(200, {"ok": True}, {PAYMENT_RESPONSE_HEADER: settled.header})


def _claim_reply(remedy: str, **extra) -> Reply:
    return Reply(200, {"remedy": remedy, **extra}, {})


def handle_claim(db, slug: str, body: Any) -> Reply:
    """A buyer's guarantee claim: check it against our records and the 90% rule, then remedy."""
    pkg = _package(db, slug)
    if pkg is None or not isinstance(body, dict):
        return Reply(404, {"error": "Package not found"}, {})
    token = str(body.get("claim_token") or "")
    sale = db.conn.execute("SELECT * FROM package_sales WHERE package_id = ? AND claim_token_hash = ?",
                           (pkg["id"], hash_token(token))).fetchone() if token else None
    if sale is None:
        return Reply(403, {"error": "Unknown sale; send the claim_token from your unlock"}, {})
    rules = verify.DEFAULT_RULES.merged(json.loads(pkg["rules"]))
    in_time = db.conn.execute("SELECT CURRENT_TIMESTAMP < ?::timestamp + make_interval(days => ?) AS ok",
                              (sale["created_at"], rules.window_days + rules.claim_days)).fetchone()["ok"]
    c = db.conn
    sold = {r["lead_id"]: dict(r) for r in c.execute(
        """SELECT pl.lead_id, pl.prospect_id, sl.published_lead_id, sl.replacement, sl.claim_id
           FROM sale_leads sl JOIN published_leads pl ON pl.id = sl.published_lead_id WHERE sl.sale_id = ?""",
        (sale["id"],)).fetchall()}
    failed = body.get("failed") if isinstance(body.get("failed"), list) else []
    reported = {str(f.get("lead_id")) for f in failed[:MAX_CLAIM_BODY_LEADS] if isinstance(f, dict)}

    def record(status: str, reason: str, shortfall: int = 0, accepted=()) -> int:
        with c.raw.transaction():
            claim_id = c.execute(
                """INSERT INTO incoming_claims (sale_id, status, shortfall, failed, reason)
                   VALUES (?, ?, ?, ?, ?) RETURNING id""",
                (sale["id"], status, shortfall, json.dumps(failed[:MAX_CLAIM_BODY_LEADS]), reason[:500])).fetchone()["id"]
            if accepted:
                lead_ids = [sold[l]["published_lead_id"] for l in accepted]
                c.execute("UPDATE sale_leads SET claim_id = ? WHERE sale_id = ? AND published_lead_id = ANY(?)",
                          (claim_id, sale["id"], lead_ids))
                royalties.claw_back(db, c, sale["id"], lead_ids)  # failed leads earn their reps nothing
            db._audit(c, None, f"selling.claim_{status}", "package_sale", sale["id"], {"shortfall": shortfall})
        return claim_id

    if not in_time:
        record("disputed", "Filed after the verification window and claim period")
        return _claim_reply("disputed", reason="The verification window and claim period have closed")

    # Our own check: a lead we've spoken to since the sale isn't a dead lead.
    reached = {r["prospect_id"] for r in c.execute(
        """SELECT DISTINCT prospect_id FROM call_log WHERE prospect_id = ANY(?) AND called_at >= ?::timestamp
             AND outcome = ANY(?)""",
        ([sold[l]["prospect_id"] for l in reported if l in sold], sale["created_at"],
         list(contact_depth.SPOKE_TO_PERSON))).fetchall()}
    accepted = [l for l in reported if l in sold and not sold[l]["replacement"] and not sold[l]["claim_id"]
                and sold[l]["prospect_id"] not in reached]
    originals = [l for l in sold.values() if not l["replacement"]]
    n = len(originals)
    failed_total = sum(1 for l in originals if l["claim_id"]) + len(accepted)
    remedied = c.execute("SELECT COALESCE(SUM(shortfall), 0) AS s FROM incoming_claims WHERE sale_id = ? "
                         "AND status IN ('replaced', 'partial', 'review', 'refunded')", (sale["id"],)).fetchone()["s"]
    allowed = max(0, -(-9 * n // 10) - (n - failed_total)) - remedied
    try:
        asked = int(body.get("shortfall") or allowed)
    except (TypeError, ValueError):
        asked = allowed
    shortfall = max(0, min(asked, allowed))
    if not accepted or shortfall <= 0:
        why = ("We've reached these leads since the sale" if reported and reached
               else "The reported leads don't bring the package below 90%")
        record("disputed", why)
        return _claim_reply("disputed", reason=why)

    exclude = {l["prospect_id"] for l in sold.values()}
    fresh = []
    for pid in candidates(db, json.loads(pkg["criteria"]), exclude=exclude, limit=shortfall * 5):
        ok, tier, _ = _check_lead(db, pid, pkg["guarantee_tier"])
        if ok:
            fresh.append((pid, tier))
        if len(fresh) == shortfall:
            break
    status = "replaced" if len(fresh) == shortfall else ("partial" if fresh else "review")
    claim_id = record(status, "" if status == "replaced" else f"{shortfall - len(fresh)} lead(s) to refund by hand",
                      shortfall, accepted)
    leads = []
    with c.raw.transaction():
        for pid, tier in fresh:
            row = c.execute("SELECT id, lead_id FROM published_leads WHERE package_id = ? AND prospect_id = ?",
                            (pkg["id"], pid)).fetchone()
            if row is None:
                row = c.execute(
                    """INSERT INTO published_leads (package_id, prospect_id, lead_id, tier, replacement)
                       VALUES (?, ?, ?, ?, 1) RETURNING id, lead_id""",
                    (pkg["id"], pid, f"L{secrets.token_hex(8)}", tier)).fetchone()
            c.execute("INSERT INTO sale_leads (sale_id, published_lead_id, replacement) VALUES (?, ?, 1)",
                      (sale["id"], row["id"]))
            leads.append({"id": row["id"], "lead_id": row["lead_id"], "prospect_id": pid, "tier": tier})
        c.execute("UPDATE incoming_claims SET replacement_count = ? WHERE id = ?", (len(fresh), claim_id))
    if not fresh:
        return _claim_reply("review", reason="No replacement leads are available; the seller will review a refund")
    return _claim_reply("replacement", leads=[p for p in (_lead_payload(db, l) for l in leads) if p])


def record_refund(db, user, claim_id: int, amount_usd: Any, tx_hash: str) -> str:
    """An owner records a refund they sent by hand for a claim under review. Returns an error or ""."""
    if not re.fullmatch(r"0x[0-9a-fA-F]{64}", tx_hash or ""):
        return "Enter the refund's 0x transaction hash"
    amount = usd_to_atomic(amount_usd)
    if amount <= 0:
        return "Enter the amount refunded"
    c = db.conn
    with c.raw.transaction():
        claim = c.execute("SELECT id FROM incoming_claims WHERE id = ? AND status IN ('review', 'partial') FOR UPDATE",
                          (claim_id,)).fetchone()
        if claim is None:
            return "That claim isn't waiting for a refund"
        c.execute("""UPDATE incoming_claims SET status = 'refunded', refund_atomic = ?, refund_tx = ?,
                     resolved_at = CURRENT_TIMESTAMP WHERE id = ?""", (amount, tx_hash, claim_id))
        c.execute("""INSERT INTO spend (kind, ref, amount_atomic, asset, network, tx_hash, status, user_id)
                     VALUES ('refund_out', ?, ?, ?, ?, ?, 'settled', ?)""",
                  (f"claim_in:{claim_id}", amount, USDC[caip()], caip(), tx_hash, getattr(user, "id", None)))
        db._audit(c, user, "selling.refund", "incoming_claim", claim_id, {"amount_atomic": amount, "tx_hash": tx_hash})
    return ""


def overview(db) -> dict:
    """The owner's selling page: packages with sales and income, and claims to handle."""
    packages = [dict(r) for r in db.conn.execute(
        """SELECT pp.*, (SELECT COUNT(*) FROM published_leads pl WHERE pl.package_id = pp.id AND pl.replacement = 0) AS leads,
                  (SELECT COUNT(*) FROM package_sales s WHERE s.package_id = pp.id) AS sales,
                  (SELECT COALESCE(SUM(s.amount_atomic), 0) FROM package_sales s WHERE s.package_id = pp.id) AS unlock_income,
                  (SELECT COALESCE(SUM(r.amount_atomic), 0) FROM sale_royalties r JOIN package_sales s ON s.id = r.sale_id
                   WHERE s.package_id = pp.id) AS royalty_income
           FROM published_packages pp ORDER BY pp.created_at DESC""").fetchall()]
    claims = [dict(r) for r in db.conn.execute(
        """SELECT ic.*, pp.title, s.payer FROM incoming_claims ic JOIN package_sales s ON s.id = ic.sale_id
           JOIN published_packages pp ON pp.id = s.package_id ORDER BY ic.created_at DESC LIMIT 100""").fetchall()]
    return {"packages": packages, "claims": claims}
