"""
x402 payments with campaign budgets and a spend ledger.

A paid resource answers 402 with a PAYMENT-REQUIRED header (base64 JSON
quote). We check the quote against what we expected to pay and the
campaign's budget, sign a USDC payment with the configured payer, retry
with PAYMENT-SIGNATURE, and read the settlement (tx hash) from
PAYMENT-RESPONSE. Every attempt is written to the `spend` table.

Safety rules:

- Off unless AGENCY_OS_X402 is "on"; mainnet also needs
  AGENCY_OS_X402_ALLOW_MAINNET=1. Campaigns opt in via `lead_packages.enabled`.
- Only USDC on the campaign's network, only to the expected pay-to address,
  and never more than the expected price or the per-kind cap.
- Budget is reserved before paying: a short transaction under a
  per-campaign advisory lock sums pending + settled spend and inserts a
  `pending` row. The network call happens outside any transaction, so slow
  facilitators don't hold locks, and concurrent runs can't overspend.
- A unique index on (kind, campaign_id, ref) for pending/settled rows makes
  each unlock and each royalty payable once, even across workers.
- Never raises: returns a PaidResult with status settled / failed / refused.
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional, Protocol

import httpx
import psycopg

from core.db import Database

# ── Networks and assets ────────────────────────────────────────────────

BASE_SEPOLIA = "eip155:84532"
BASE_MAINNET = "eip155:8453"
NETWORKS = {
    "base-sepolia": BASE_SEPOLIA,
    "base": BASE_MAINNET,
}
MAINNETS = {"base"}

# USDC per CAIP-2 network (6 decimals). Same addresses as x402's defaults.
USDC = {
    BASE_SEPOLIA: "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
    BASE_MAINNET: "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
}
USDC_DECIMALS = 6

EXPLORERS = {
    BASE_SEPOLIA: "https://sepolia.basescan.org/tx/",
    BASE_MAINNET: "https://basescan.org/tx/",
}

PAYMENT_REQUIRED_HEADER = "PAYMENT-REQUIRED"
PAYMENT_SIGNATURE_HEADER = "PAYMENT-SIGNATURE"
PAYMENT_RESPONSE_HEADER = "PAYMENT-RESPONSE"

_SPEND_LOCK_NS = 7_201_200  # advisory lock namespace: (ns, campaign_id)
_MAX_HEADER_BYTES = 64 * 1024


def usd_to_atomic(usd) -> int:
    """Dollars to USDC atomic units, rounded down to a whole unit."""
    try:
        value = float(usd)
    except (TypeError, ValueError):
        return 0
    return max(0, int(value * 10 ** USDC_DECIMALS))


def atomic_to_usd(atomic) -> float:
    return int(atomic or 0) / 10 ** USDC_DECIMALS


def explorer_url(network: str, tx_hash: str) -> str:
    base = EXPLORERS.get(network or "")
    return f"{base}{tx_hash}" if base and tx_hash else ""


def payments_enabled() -> bool:
    return os.environ.get("AGENCY_OS_X402", "").strip().lower() in ("1", "on", "true", "yes")


def mainnet_allowed() -> bool:
    return os.environ.get("AGENCY_OS_X402_ALLOW_MAINNET", "").strip() == "1"


# ── Policy ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SpendPolicy:
    """A campaign's spending rules, from the `lead_packages:` block of campaign.yaml."""

    enabled: bool = False
    network: str = "base-sepolia"
    max_unlock_atomic: int = 0
    max_royalty_atomic: int = 0
    monthly_budget_atomic: int = 0

    @classmethod
    def from_config(cls, block: Optional[dict]) -> "SpendPolicy":
        block = block or {}
        network = str(block.get("network") or "base-sepolia")
        return cls(
            enabled=bool(block.get("enabled", False)),
            network=network if network in NETWORKS else "base-sepolia",
            max_unlock_atomic=usd_to_atomic(block.get("max_unlock_usd", 0)),
            max_royalty_atomic=usd_to_atomic(block.get("max_royalty_per_contact_usd", 0)),
            monthly_budget_atomic=usd_to_atomic(block.get("monthly_budget_usd", 0)),
        )

    @property
    def caip(self) -> str:
        return NETWORKS[self.network]

    def cap_for(self, kind: str) -> int:
        return {"unlock": self.max_unlock_atomic, "royalty": self.max_royalty_atomic}.get(kind, 0)

    def problem(self) -> Optional[str]:
        """Why this policy can't pay right now, or None."""
        if not payments_enabled():
            return "x402 payments are turned off (AGENCY_OS_X402)"
        if not self.enabled:
            return "Lead packages are not enabled for this campaign"
        if self.network in MAINNETS and not mainnet_allowed():
            return "Mainnet payments need AGENCY_OS_X402_ALLOW_MAINNET=1"
        return None


@dataclass(frozen=True)
class Expected:
    """What the caller agreed to pay, from the provider's catalog."""

    pay_to: str
    max_atomic: int


# ── Quotes ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Quote:
    """One accepted payment option from a PAYMENT-REQUIRED header."""

    payment_required: dict   # the full decoded header, narrowed to `accept`
    accept: dict
    amount_atomic: int
    asset: str
    network: str
    pay_to: str


def _b64json(value: str) -> Any:
    if len(value) > _MAX_HEADER_BYTES:
        raise ValueError("payment header too large")
    padded = value + "=" * (-len(value) % 4)
    return json.loads(base64.b64decode(padded, validate=False))


def _acceptable_amount(accept: Any, policy: SpendPolicy) -> int:
    """The amount of a USDC 'exact' option on the policy's network, or 0 if unusable."""
    if not isinstance(accept, dict) or not accept.get("payTo"):
        return 0
    if accept.get("scheme") != "exact" or accept.get("network") != policy.caip:
        return 0
    if str(accept.get("asset", "")).lower() != USDC[policy.caip].lower():
        return 0
    try:
        amount = int(str(accept.get("amount", "")))
        timeout = int(accept.get("maxTimeoutSeconds"))
    except (TypeError, ValueError):
        return 0
    return amount if amount > 0 and timeout > 0 else 0


def parse_quote(response: httpx.Response, policy: SpendPolicy) -> Quote:
    """Pick the USDC 'exact' option on the policy's network from a 402 response.

    Raises ValueError if there is no acceptable option. Only x402 v2 is
    supported (v1's X-PAYMENT flow is refused).
    """
    header = response.headers.get(PAYMENT_REQUIRED_HEADER)
    if not header:
        raise ValueError("402 without a PAYMENT-REQUIRED header")
    data = _b64json(header)
    if not isinstance(data, dict) or int(data.get("x402Version", 0)) != 2:
        raise ValueError("Only x402 version 2 is supported")
    for accept in data.get("accepts") or []:
        amount = _acceptable_amount(accept, policy)
        if amount:
            narrowed = {**data, "accepts": [accept]}
            return Quote(narrowed, accept, amount, accept["asset"], policy.caip, str(accept["payTo"]))
    raise ValueError(f"No USDC payment option on {policy.network}")


def parse_settlement(response: httpx.Response) -> dict:
    header = response.headers.get(PAYMENT_RESPONSE_HEADER)
    if not header:
        return {}
    data = _b64json(header)
    return data if isinstance(data, dict) else {}


def quote_problem(quote: Quote, expected: Expected, policy: SpendPolicy, kind: str) -> Optional[str]:
    """Why we won't pay this quote, or None."""
    if quote.pay_to.lower() != expected.pay_to.lower():
        return "Quote pays a different address than the catalog lists"
    if quote.amount_atomic > expected.max_atomic:
        return "Quote is higher than the catalog price"
    if quote.amount_atomic > policy.cap_for(kind):
        return f"Quote is over the campaign's {kind} cap"
    return None


# ── Payers ─────────────────────────────────────────────────────────────


class Payer(Protocol):
    """Signs a payment for one quote. Returns headers for the retry request."""

    def is_configured(self) -> bool: ...

    def payment_headers(self, quote: Quote) -> dict[str, str]: ...


class CdpPayer:
    """Coinbase CDP server wallet, signing through the x402 SDK.

    Needs CDP_API_KEY_ID, CDP_API_KEY_SECRET and CDP_WALLET_SECRET (read by
    the CDP SDK) and optionally AGENCY_OS_CDP_ACCOUNT (default "agency-os").
    The SDKs are imported lazily so the app runs without them.
    """

    def __init__(self, account_name: str = ""):
        self.account_name = account_name or os.environ.get("AGENCY_OS_CDP_ACCOUNT", "agency-os")
        self._account = None

    def is_configured(self) -> bool:
        return all(os.environ.get(k) for k in ("CDP_API_KEY_ID", "CDP_API_KEY_SECRET", "CDP_WALLET_SECRET"))

    def _local_account(self):
        if self._account is None:
            import asyncio
            import threading

            from cdp import CdpClient, EvmLocalAccount

            async def load():
                async with CdpClient() as cdp:
                    server_account = await cdp.evm.get_or_create_account(name=self.account_name)
                    return EvmLocalAccount(server_account)

            # Run in a fresh thread: callers may already be inside an event loop.
            box: dict = {}
            thread = threading.Thread(target=lambda: box.update(account=asyncio.run(load())))
            thread.start()
            thread.join(timeout=60)
            if "account" not in box:
                raise RuntimeError("Could not load the CDP wallet account")
            self._account = box["account"]
        return self._account

    @property
    def address(self) -> str:
        return self._local_account().address

    def payment_headers(self, quote: Quote) -> dict[str, str]:
        from x402 import x402ClientSync
        from x402.http.utils import encode_payment_signature_header
        from x402.mechanisms.evm.exact import register_exact_evm_client
        from x402.mechanisms.evm.signers import EthAccountSigner
        from x402.schemas.payments import PaymentRequired

        client = x402ClientSync()
        # Our own policy already checked asset, payee and amount.
        client.set_spend_controls(False)
        register_exact_evm_client(client, EthAccountSigner(self._local_account()), networks=[quote.network])
        payload = client.create_payment_payload(PaymentRequired.model_validate(quote.payment_required))
        return {PAYMENT_SIGNATURE_HEADER: encode_payment_signature_header(payload)}


_default_payer: Optional[Payer] = None


def default_payer() -> Payer:
    global _default_payer
    if _default_payer is None:
        _default_payer = CdpPayer()
    return _default_payer


# ── Ledger ─────────────────────────────────────────────────────────────


@dataclass
class PaidResult:
    status: str                      # settled | failed | refused | free
    spend_id: Optional[int] = None
    amount_atomic: int = 0
    tx_hash: str = ""
    network: str = ""
    error: str = ""
    response: Optional[httpx.Response] = field(default=None, repr=False)

    @property
    def ok(self) -> bool:
        return self.status in ("settled", "free")


def _month_start() -> str:
    now = datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


def reserve(db: Database, policy: SpendPolicy, *, kind: str, ref: str, campaign_id: int,
            user_id: Optional[int], amount_atomic: int, quote: Quote, url: str,
            prospect_id: Optional[int] = None, lead_package_id: Optional[int] = None) -> tuple[Optional[int], str, bool]:
    """Check budgets and insert a pending spend row.

    Returns (spend_id, refusal reason, already paid or in progress).
    """
    c = db.conn
    try:
        with c.raw.transaction():
            c.execute("SELECT pg_advisory_xact_lock(?, ?)", (_SPEND_LOCK_NS, campaign_id))
            since = _month_start()
            campaign_total = c.execute(
                """SELECT COALESCE(SUM(amount_atomic), 0) AS total FROM spend
                   WHERE campaign_id = ? AND status IN ('pending', 'settled') AND created_at >= ?""",
                (campaign_id, since),
            ).fetchone()["total"]
            if campaign_total + amount_atomic > policy.monthly_budget_atomic:
                return None, "Over the campaign's monthly budget", False
            if user_id is not None:
                allowance = c.execute(
                    "SELECT monthly_spend_allowance_atomic AS a FROM user_prefs WHERE user_id = ?",
                    (user_id,),
                ).fetchone()
                user_total = c.execute(
                    """SELECT COALESCE(SUM(amount_atomic), 0) AS total FROM spend
                       WHERE user_id = ? AND status IN ('pending', 'settled') AND created_at >= ?""",
                    (user_id, since),
                ).fetchone()["total"]
                if user_total + amount_atomic > (allowance["a"] if allowance else 0):
                    return None, "Over your monthly spend allowance", False
            row = c.execute(
                """INSERT INTO spend (kind, ref, campaign_id, user_id, prospect_id, lead_package_id,
                       url, amount_atomic, asset, network, pay_to, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending') RETURNING id""",
                (kind, ref, campaign_id, user_id, prospect_id, lead_package_id,
                 url, amount_atomic, quote.asset, quote.network, quote.pay_to),
            ).fetchone()
            return row["id"], "", False
    except psycopg.errors.UniqueViolation:
        return None, "Already paid (or a payment is in progress)", True


def finish(db: Database, spend_id: int, status: str, tx_hash: str = "", error: str = "") -> None:
    db.conn.execute(
        """UPDATE spend SET status = ?, tx_hash = ?, error = ?, updated_at = CURRENT_TIMESTAMP
           WHERE id = ?""",
        (status, tx_hash or None, (error or "")[:500] or None, spend_id),
    )


def record_refusal(db: Database, *, kind: str, ref: str, campaign_id: int, user_id: Optional[int],
                   url: str, error: str, amount_atomic: int = 0, quote: Optional[Quote] = None,
                   prospect_id: Optional[int] = None, lead_package_id: Optional[int] = None) -> None:
    db.conn.execute(
        """INSERT INTO spend (kind, ref, campaign_id, user_id, prospect_id, lead_package_id,
               url, amount_atomic, asset, network, pay_to, status, error)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'refused', ?)""",
        (kind, ref, campaign_id, user_id, prospect_id, lead_package_id, url, amount_atomic,
         quote.asset if quote else None, quote.network if quote else None,
         quote.pay_to if quote else None, error[:500]),
    )


# ── The paid request ───────────────────────────────────────────────────


def paid_request(db: Database, http: httpx.Client, payer: Payer, policy: SpendPolicy, *,
                 kind: str, ref: str, campaign_id: int, user_id: Optional[int],
                 method: str, url: str, expected: Expected, json_body: Any = None,
                 prospect_id: Optional[int] = None, lead_package_id: Optional[int] = None) -> PaidResult:
    """Request a resource, paying its x402 quote if it's within policy. Never raises."""
    ids = dict(kind=kind, ref=ref, campaign_id=campaign_id, user_id=user_id, url=url,
               prospect_id=prospect_id, lead_package_id=lead_package_id)

    def refuse(reason: str, quote: Optional[Quote] = None) -> PaidResult:
        record_refusal(db, error=reason, quote=quote,
                       amount_atomic=quote.amount_atomic if quote else 0, **ids)
        return PaidResult("refused", error=reason)

    problem = policy.problem()
    if problem:
        return refuse(problem)
    if not payer.is_configured():
        return refuse("No payment wallet is configured")

    try:
        first = http.request(method, url, json=json_body)
    except httpx.HTTPError as exc:
        return PaidResult("failed", error=f"Request failed: {exc}")
    if first.status_code != 402:
        if first.is_success:
            return PaidResult("free", response=first)
        return PaidResult("failed", error=f"Provider returned {first.status_code}", response=first)

    try:
        quote = parse_quote(first, policy)
    except (ValueError, KeyError, TypeError) as exc:
        return refuse(f"Unusable payment quote: {exc}")
    problem = quote_problem(quote, expected, policy, kind)
    if problem:
        return refuse(problem, quote)

    spend_id, reason, duplicate = reserve(db, policy, amount_atomic=quote.amount_atomic, quote=quote, **ids)
    if duplicate:
        return PaidResult("refused", error=reason)  # the existing row already records it
    if spend_id is None:
        return refuse(reason, quote)

    try:
        headers = payer.payment_headers(quote)
    except Exception as exc:  # signing must never take the caller down
        finish(db, spend_id, "failed", error=f"Signing failed: {exc}")
        return PaidResult("failed", spend_id=spend_id, error=f"Signing failed: {exc}")

    try:
        paid = http.request(method, url, json=json_body, headers=headers)
    except httpx.HTTPError as exc:
        # The payment may or may not have settled; leave it pending so it still
        # counts against the budget until someone reconciles it.
        finish(db, spend_id, "pending", error=f"No response after paying: {exc}")
        return PaidResult("failed", spend_id=spend_id, error=f"No response after paying: {exc}")
    return _record_settlement(db, spend_id, quote, paid)


def _record_settlement(db: Database, spend_id: int, quote: Quote, paid: httpx.Response) -> PaidResult:
    """Mark the spend row settled (with its tx hash) or failed, from PAYMENT-RESPONSE."""
    try:
        settlement = parse_settlement(paid)
    except (ValueError, TypeError):
        settlement = {}
    tx_hash = str(settlement.get("transaction") or "")
    if paid.is_success and settlement.get("success") and tx_hash:
        finish(db, spend_id, "settled", tx_hash=tx_hash)
        return PaidResult("settled", spend_id, quote.amount_atomic, tx_hash, quote.network, response=paid)

    error = settlement.get("errorReason") or settlement.get("errorMessage") or f"HTTP {paid.status_code}"
    finish(db, spend_id, "failed", tx_hash=tx_hash, error=str(error))
    return PaidResult("failed", spend_id, quote.amount_atomic, tx_hash, quote.network,
                      error=str(error), response=paid)
