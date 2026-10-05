"""
Rep royalties: the reps who built a lead we sell share in what buyers pay for it.

Credit (lead_contributions), worked out from what we already record:

- enriched:  the rep whose contact edit supplied the lead's current email or phone
- qualified: the rep who logged a call that reached the decision-maker
- sourced:   set by an owner on the prospect page (imports have no person behind them)

Owners can add or remove credit on the prospect page; a removed credit stays
removed. Several reps can share a task.

Accruals (rep_accruals): every time a buyer pays for a lead (its share of the
unlock, and its royalty), the package's contributor pool % of that money is
split by task weight, enriched 50 / qualified 30 / sourced 20, equally among
that task's reps. A task nobody did stays with the house. Rounding always
favors the house.

Accruals are `pending` until the buyer's verification window and claim
period end, then `payable`. If the buyer's claim on a lead is accepted, its
pending accruals are reversed, and any already payable or paid are taken back
with a negative line that carries forward.

Payouts (owner, Administration -> Rep Payouts): balances at or above
AGENCY_OS_PAYOUT_MIN_USD (default $5) are paid in USDC to the address each
rep set on their Account page. With AGENCY_OS_PAYOUTS=on and the CDP wallet,
"Pay" sends it (capped by AGENCY_OS_PAYOUT_MAX_USD, default $500, per payout);
otherwise an owner pays by hand and records the transaction.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
from typing import Optional, Protocol

from core.contact_depth import SPOKE_TO_PERSON
from core.payments import MAINNETS, NETWORKS, USDC, mainnet_allowed, usd_to_atomic

TASK_WEIGHTS = {"enriched": 50, "qualified": 30, "sourced": 20}
TASK_LABELS = {"enriched": "Found the contact", "qualified": "Reached the decision-maker", "sourced": "Sourced the lead"}
_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_TX_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")


# ── Credit ─────────────────────────────────────────────────────────────


def refresh_credit(db, prospect_ids: list[int]) -> None:
    """Add credit our records show (enriched, qualified); never re-adds credit an owner removed."""
    if not prospect_ids:
        return
    c = db.conn
    rows = []
    for o in c.execute("""SELECT prospect_id, LOWER(contact_email) AS email, contact_phone FROM outreach
                          WHERE prospect_id = ANY(?)""", (prospect_ids,)).fetchall():
        for a in c.execute(
                """SELECT actor_id, details FROM audit_log WHERE action = 'prospect.contact' AND actor_id IS NOT NULL
                     AND (details::jsonb ->> 'prospect_id')::int = ?""", (o["prospect_id"],)).fetchall():
            d = json.loads(a["details"] or "{}")
            if (o["email"] and str(d.get("contact_email", "")).lower() == o["email"]) or \
               (o["contact_phone"] and d.get("contact_phone") == o["contact_phone"]):
                rows.append((o["prospect_id"], a["actor_id"], "enriched"))
    for call in c.execute(
            """SELECT DISTINCT prospect_id, called_by_user_id FROM call_log WHERE prospect_id = ANY(?)
                 AND called_by_user_id IS NOT NULL AND outcome = ANY(?)
                 AND COALESCE(TRIM(decision_maker_name), '') <> ''""",
            (prospect_ids, list(SPOKE_TO_PERSON))).fetchall():
        rows.append((call["prospect_id"], call["called_by_user_id"], "qualified"))
    if rows:
        c.executemany("""INSERT INTO lead_contributions (prospect_id, user_id, task, source) VALUES (?, ?, ?, 'auto')
                         ON CONFLICT (prospect_id, user_id, task) DO NOTHING""", rows)


def credits(db, prospect_id: int) -> list[dict]:
    refresh_credit(db, [prospect_id])
    return [dict(r) for r in db.conn.execute(
        """SELECT lc.*, u.name, u.email FROM lead_contributions lc JOIN users u ON u.id = lc.user_id
           WHERE lc.prospect_id = ? ORDER BY lc.task, u.name""", (prospect_id,)).fetchall()]


def set_credit(db, actor, prospect_id: int, user_id: int, task: str, active: bool) -> None:
    """An owner gives or removes credit (audited)."""
    if task not in TASK_WEIGHTS:
        raise ValueError("Unknown task")
    c = db.conn
    with c.raw.transaction():
        c.execute(
            """INSERT INTO lead_contributions (prospect_id, user_id, task, source, active, created_by)
               VALUES (?, ?, ?, 'manual', ?, ?)
               ON CONFLICT (prospect_id, user_id, task) DO UPDATE SET active = EXCLUDED.active, source = 'manual'""",
            (prospect_id, user_id, task, int(active), getattr(actor, "id", None)))
        db._audit(c, actor, "royalties.credit", "prospect", prospect_id,
                  {"user_id": user_id, "task": task, "active": active})


# ── Accruals ───────────────────────────────────────────────────────────


def split(pool: int, contributors: dict[str, list[int]]) -> dict[tuple[int, str], int]:
    """{(user_id, task): atomic} for a pool. Missing tasks and rounding stay with the house."""
    out: dict[tuple[int, str], int] = {}
    for task, weight in TASK_WEIGHTS.items():
        reps = contributors.get(task) or []
        if not reps:
            continue
        each = pool * weight // 100 // len(reps)
        for user_id in reps:
            if each > 0:
                out[(user_id, task)] = out.get((user_id, task), 0) + each
    return out


def accrue(db, c, sale_id: int, income: str, amounts: dict[int, int]) -> int:
    """Accrue the contributor pool on money received; run inside the sale's transaction.

    amounts: {published_lead_id: atomic received for that lead}. Returns the total accrued.
    """
    sale = c.execute(
        """SELECT s.created_at, pp.contributor_pool_pct, pp.rules FROM package_sales s
           JOIN published_packages pp ON pp.id = s.package_id WHERE s.id = ?""", (sale_id,)).fetchone()
    if sale is None or not sale["contributor_pool_pct"] or not amounts:
        return 0
    rules = json.loads(sale["rules"])
    days = int(rules.get("window_days", 30)) + int(rules.get("claim_days", 7))
    leads = {r["id"]: r["prospect_id"] for r in c.execute(
        "SELECT id, prospect_id FROM published_leads WHERE id = ANY(?)", (list(amounts),)).fetchall()}
    refresh_credit(db, list(set(leads.values())))
    credit: dict[int, dict[str, list[int]]] = {}
    for r in c.execute("""SELECT prospect_id, user_id, task FROM lead_contributions
                          WHERE prospect_id = ANY(?) AND active = 1""", (list(set(leads.values())),)).fetchall():
        credit.setdefault(r["prospect_id"], {}).setdefault(r["task"], []).append(r["user_id"])
    rows = []
    for lead_id, received in amounts.items():
        pool = received * sale["contributor_pool_pct"] // 100
        for (user_id, task), amount in split(pool, credit.get(leads.get(lead_id), {})).items():
            rows.append((user_id, sale_id, lead_id, income, task, amount, sale["created_at"], days))
    c.executemany(
        """INSERT INTO rep_accruals (user_id, sale_id, published_lead_id, income, task, amount_atomic, status, payable_at)
           VALUES (?, ?, ?, ?, ?, ?, 'pending', ?::timestamp + make_interval(days => ?))
           ON CONFLICT (sale_id, published_lead_id, income, user_id, task) DO NOTHING""", rows)
    return sum(r[5] for r in rows)


def claw_back(db, c, sale_id: int, published_lead_ids: list[int]) -> None:
    """A buyer's claim on these leads held: reverse what's pending, take back what isn't."""
    if not published_lead_ids:
        return
    c.execute("""UPDATE rep_accruals SET status = 'reversed' WHERE sale_id = ? AND published_lead_id = ANY(?)
                 AND status = 'pending' AND amount_atomic > 0""", (sale_id, published_lead_ids))
    for r in c.execute("""SELECT * FROM rep_accruals WHERE sale_id = ? AND published_lead_id = ANY(?)
                          AND status IN ('payable', 'paid') AND amount_atomic > 0""",
                       (sale_id, published_lead_ids)).fetchall():
        c.execute(
            """INSERT INTO rep_accruals (user_id, sale_id, published_lead_id, income, task, amount_atomic, status, payable_at)
               VALUES (?, ?, ?, ?, ?, ?, 'payable', CURRENT_TIMESTAMP)
               ON CONFLICT (sale_id, published_lead_id, income, user_id, task) DO NOTHING""",
            (r["user_id"], sale_id, r["published_lead_id"], f"clawback_{r['income']}", r["task"], -r["amount_atomic"]))


def _mature(db) -> None:
    db.conn.execute("UPDATE rep_accruals SET status = 'payable' WHERE status = 'pending' AND payable_at <= CURRENT_TIMESTAMP")


def balances(db, user_id: Optional[int] = None) -> list[dict]:
    """Per rep: pending, payable (not yet in a payout) and paid, plus their payout address."""
    _mature(db)
    sql = """SELECT u.id AS user_id, u.name, u.email, up.payout_address,
                    COALESCE(SUM(a.amount_atomic) FILTER (WHERE a.status = 'pending'), 0) AS pending,
                    COALESCE(SUM(a.amount_atomic) FILTER (WHERE a.status = 'payable' AND a.payout_id IS NULL), 0) AS payable,
                    COALESCE(SUM(a.amount_atomic) FILTER (WHERE a.status = 'paid'), 0) AS paid
             FROM rep_accruals a JOIN users u ON u.id = a.user_id LEFT JOIN user_prefs up ON up.user_id = u.id"""
    params: tuple = ()
    if user_id is not None:
        sql += " WHERE a.user_id = ?"
        params = (user_id,)
    return [dict(r) for r in db.conn.execute(sql + " GROUP BY u.id, u.name, u.email, up.payout_address ORDER BY u.name",
                                             params).fetchall()]


def statement(db, user_id: int, limit: int = 50) -> list[dict]:
    _mature(db)
    return [dict(r) for r in db.conn.execute(
        """SELECT a.*, pp.title, p.name AS lead_name FROM rep_accruals a
           JOIN package_sales s ON s.id = a.sale_id JOIN published_packages pp ON pp.id = s.package_id
           JOIN published_leads pl ON pl.id = a.published_lead_id JOIN prospects p ON p.id = pl.prospect_id
           WHERE a.user_id = ? ORDER BY a.created_at DESC, a.id DESC LIMIT ?""", (user_id, limit)).fetchall()]


def set_payout_address(db, user, address: str) -> str:
    """A rep sets where their royalties go (the web route checks their password first). Returns an error or ""."""
    address = (address or "").strip()
    if address and not _ADDRESS_RE.match(address):
        return "Enter a 0x wallet address (42 characters), or leave it empty"
    c = db.conn
    with c.raw.transaction():
        c.execute("""INSERT INTO user_prefs (user_id, payout_address) VALUES (?, ?)
                     ON CONFLICT (user_id) DO UPDATE SET payout_address = EXCLUDED.payout_address,
                     updated_at = CURRENT_TIMESTAMP""", (user.id, address or None))
        db._audit(c, user, "royalties.payout_address", "user", user.id, {"address": address})
    return ""


# ── Payouts ────────────────────────────────────────────────────────────


class Sender(Protocol):
    def is_configured(self) -> bool: ...

    def send(self, to: str, amount_atomic: int, network_key: str) -> str:
        """Send USDC; returns the transaction hash. Raises on failure."""


class CdpSender:
    """Sends USDC from the CDP server wallet (the same account core/payments.CdpPayer uses)."""

    def is_configured(self) -> bool:
        return (os.environ.get("AGENCY_OS_PAYOUTS", "").lower() in ("1", "on", "true", "yes")
                and all(os.environ.get(k) for k in ("CDP_API_KEY_ID", "CDP_API_KEY_SECRET", "CDP_WALLET_SECRET")))

    def send(self, to: str, amount_atomic: int, network_key: str) -> str:
        from cdp import CdpClient

        name = os.environ.get("AGENCY_OS_CDP_ACCOUNT", "agency-os")

        async def run():
            async with CdpClient() as cdp:
                account = await cdp.evm.get_or_create_account(name=name)
                return await account.transfer(to=to, amount=amount_atomic, token="usdc", network=network_key)

        box: dict = {}

        def worker():
            try:
                box["tx"] = asyncio.run(run())
            except Exception as exc:  # reported back to the owner, never swallowed
                box["error"] = exc

        thread = threading.Thread(target=worker)  # callers may already be inside an event loop
        thread.start()
        thread.join(timeout=120)
        if "error" in box:
            raise box["error"]
        if "tx" not in box:
            raise RuntimeError("The transfer timed out")
        return str(box["tx"])


_sender: Optional[Sender] = None


def default_sender() -> Sender:
    global _sender
    if _sender is None:
        _sender = CdpSender()
    return _sender


def payout_network() -> str:
    key = os.environ.get("AGENCY_OS_SELL_NETWORK", "base-sepolia")
    return key if key in NETWORKS else "base-sepolia"


def minimum() -> int:
    return usd_to_atomic(os.environ.get("AGENCY_OS_PAYOUT_MIN_USD", "5"))


def maximum() -> int:
    return usd_to_atomic(os.environ.get("AGENCY_OS_PAYOUT_MAX_USD", "500"))


def _claim_balance(c, user_id: int) -> tuple[int, list[int]]:
    rows = c.execute("""SELECT id, amount_atomic FROM rep_accruals WHERE user_id = ? AND status = 'payable'
                        AND payout_id IS NULL FOR UPDATE""", (user_id,)).fetchall()
    return sum(r["amount_atomic"] for r in rows), [r["id"] for r in rows]


def pay(db, actor, user_id: int, *, sender: Optional[Sender] = None, tx_hash: str = "") -> str:
    """Pay one rep's payable balance: send it (sender) or record a payment made by hand (tx_hash).

    Returns an error or "". The balance is locked while paying, so it can't be paid twice.
    """
    _mature(db)
    network = payout_network()
    if network in MAINNETS and not mainnet_allowed():
        return "Mainnet payouts need AGENCY_OS_X402_ALLOW_MAINNET=1"
    if tx_hash and not _TX_RE.match(tx_hash):
        return "Enter the payout's 0x transaction hash"
    if not tx_hash and (sender is None or not sender.is_configured()):
        return "Automatic payouts are off (AGENCY_OS_PAYOUTS and the CDP wallet); pay by hand and record the transaction"
    c = db.conn
    with c.raw.transaction():
        c.execute("SELECT pg_advisory_xact_lock(?, ?)", (7_201_201, user_id))
        prefs = c.execute("SELECT payout_address FROM user_prefs WHERE user_id = ?", (user_id,)).fetchone()
        address = prefs["payout_address"] if prefs else None
        if not address:
            return "This rep hasn't set a payout address"
        amount, accrual_ids = _claim_balance(c, user_id)
        if amount < minimum():
            return f"Balance is under the ${minimum() / 1e6:,.2f} minimum"
        if not tx_hash and amount > maximum():
            return f"Balance is over the ${maximum() / 1e6:,.2f} automatic payout cap; pay by hand and record it"
        payout_id = c.execute(
            """INSERT INTO rep_payouts (user_id, amount_atomic, address, network, tx_hash, status, created_by)
               VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING id""",
            (user_id, amount, address, NETWORKS[network], tx_hash or None, "recorded" if tx_hash else "sending",
             getattr(actor, "id", None))).fetchone()["id"]
        c.execute("UPDATE rep_accruals SET payout_id = ? WHERE id = ANY(?)", (payout_id, accrual_ids))
    if not tx_hash:
        try:
            tx_hash = sender.send(address, amount, network)
        except Exception as exc:
            with c.raw.transaction():
                c.execute("UPDATE rep_payouts SET status = 'failed', error = ? WHERE id = ?", (str(exc)[:500], payout_id))
                c.execute("UPDATE rep_accruals SET payout_id = NULL WHERE payout_id = ?", (payout_id,))
            return f"The transfer failed: {exc}"
    with c.raw.transaction():
        c.execute("UPDATE rep_payouts SET tx_hash = ?, status = 'sent' WHERE id = ? AND status <> 'recorded'",
                  (tx_hash, payout_id))
        c.execute("UPDATE rep_accruals SET status = 'paid' WHERE payout_id = ?", (payout_id,))
        c.execute("""INSERT INTO spend (kind, ref, user_id, amount_atomic, asset, network, pay_to, tx_hash, status)
                     VALUES ('contributor_payout', ?, ?, ?, ?, ?, ?, ?, 'settled')""",
                  (f"payout:{payout_id}", user_id, amount, USDC[NETWORKS[network]], NETWORKS[network], address, tx_hash))
        db._audit(c, actor, "royalties.payout", "user", user_id, {"amount_atomic": amount, "tx_hash": tx_hash})
    return ""


def payouts(db, limit: int = 50) -> list[dict]:
    return [dict(r) for r in db.conn.execute(
        """SELECT p.*, u.name FROM rep_payouts p JOIN users u ON u.id = p.user_id
           ORDER BY p.created_at DESC LIMIT ?""", (limit,)).fetchall()]
