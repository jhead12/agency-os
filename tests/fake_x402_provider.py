"""
A fake x402 lead-package provider and payer for tests. No network, no wallet.

The provider speaks the real wire format: 402 + base64 JSON PAYMENT-REQUIRED,
then 200 + base64 JSON PAYMENT-RESPONSE once a PAYMENT-SIGNATURE is sent.
"""

from __future__ import annotations

import base64
import json
import threading

import httpx

from core import contact_depth
from core.payments import BASE_SEPOLIA, USDC

BASE = "https://leads.test"
PAY_TO = "0x" + "ab" * 20


def b64(data) -> str:
    return base64.b64encode(json.dumps(data).encode()).decode()


# Contact histories a seller would send, one per tier.
HISTORIES = {
    "mailed": [{"channel": "mail", "outcome": "delivered", "at": "2026-08-01"}],
    "phone_verified": [{"channel": "mail", "outcome": "delivered", "at": "2026-08-01"},
                       {"channel": "call", "outcome": "voicemail", "org_confirmed": True, "at": "2026-08-05"}],
    "pitched": [{"channel": "mail", "outcome": "delivered", "at": "2026-08-01"},
                {"channel": "call", "outcome": "completed", "decision_maker": True, "at": "2026-08-09"}],
}
ROYALTY_BY_TIER = {"mailed": "50000", "phone_verified": "250000", "pitched": "1000000"}


def lead(n: int, tier: str = "phone_verified", **extra) -> dict:
    return {
        "lead_id": f"L{n}", "name": f"Test Org {n}", "ein": f"99-00000{n:02d}",
        "city": "Los Angeles", "state": "CA", "ntee_code": "W",
        "contact_name": f"Pat Person {n}", "contact_email": f"pat{n}@org{n}.example",
        "contact_title": "Executive Director", "tier": tier,
        "contact_history": HISTORIES.get(tier, []), **extra,
    }


class FakeProvider:
    """Catalog + paid endpoints. Tweak attributes to simulate bad providers."""

    def __init__(self):
        self.packages = {
            "p1": {"id": "p1", "title": "LA civic nonprofits", "industry": "Civic", "region": "CA",
                   "lead_count": 3, "unlock_price_atomic": "5000000", "royalty_atomic": "100000",
                   "royalty_by_tier": dict(ROYALTY_BY_TIER),
                   "tier_mix": {"phone_verified": 2, "pitched": 1},
                   "pay_to": PAY_TO, "network": BASE_SEPOLIA, "sms_consent": False,
                   "consent_note": "Public records; emailed opt-in 2026",
                   "guarantee": {"verified_rate_min": 0.9, "window_days": 30, "tier": "phone_verified"}},
        }
        self.leads = {"p1": [lead(1), lead(2), lead(3, "pitched")]}
        self.quote_amount: dict[str, int] = {}     # override what the 402 asks for
        self.quote_pay_to = ""                     # override the payee in the 402
        self.settle_success = True
        self.paid_calls: list[tuple[str, str]] = []
        # What a guarantee claim gets back; tests swap in refund / disputed answers.
        self.claim_response: dict = {"remedy": "replacement", "leads": [lead(20), lead(21), lead(22)]}
        self.claims: list[dict] = []
        self.lock = threading.Lock()

    def add_package(self, package_id: str, price_atomic: int, royalty_atomic: int = 100000, leads=None):
        self.packages[package_id] = {
            **self.packages["p1"], "id": package_id, "title": f"Package {package_id}",
            "unlock_price_atomic": str(price_atomic), "royalty_atomic": str(royalty_atomic),
        }
        self.leads[package_id] = leads if leads is not None else [lead(10 + len(self.leads))]

    def _quote(self, key: str, default_amount: str) -> httpx.Response:
        amount = str(self.quote_amount.get(key, default_amount))
        body = {"x402Version": 2, "error": "payment required", "accepts": [{
            "scheme": "exact", "network": BASE_SEPOLIA, "asset": USDC[BASE_SEPOLIA],
            "amount": amount, "payTo": self.quote_pay_to or PAY_TO,
            "maxTimeoutSeconds": 60, "extra": {"name": "USDC", "version": "2"},
        }]}
        return httpx.Response(402, headers={"PAYMENT-REQUIRED": b64(body)}, json={})

    def _settled(self, payload) -> httpx.Response:
        tx = "0x" + f"{len(self.paid_calls):064x}"
        settle = {"success": self.settle_success, "transaction": tx if self.settle_success else "",
                  "network": BASE_SEPOLIA, **({} if self.settle_success else {"errorReason": "insufficient_funds"})}
        return httpx.Response(200 if self.settle_success else 402,
                              headers={"PAYMENT-RESPONSE": b64(settle)}, json=payload)

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        paid = "PAYMENT-SIGNATURE" in request.headers
        if request.method == "GET" and path == "/packages":
            return httpx.Response(200, json={"packages": list(self.packages.values())})
        parts = path.strip("/").split("/")
        if len(parts) != 3 or parts[0] != "packages" or parts[1] not in self.packages:
            return httpx.Response(404)
        package = self.packages[parts[1]]
        if parts[2] == "leads" and request.method == "GET":
            if not paid:
                return self._quote(f"unlock:{parts[1]}", package["unlock_price_atomic"])
            with self.lock:
                self.paid_calls.append(("unlock", parts[1]))
            return self._settled({"leads": self.leads[parts[1]]})
        if parts[2] == "contacts" and request.method == "POST":
            if not paid:
                return self._quote(f"royalty:{parts[1]}", self._royalty(parts[1], json.loads(request.content)))
            with self.lock:
                self.paid_calls.append(("royalty", json.loads(request.content)["lead_id"]))
            return self._settled({"ok": True})
        if parts[2] == "claims" and request.method == "POST":
            self.claims.append(json.loads(request.content))
            return httpx.Response(200, json=self.claim_response)
        return httpx.Response(405)

    def _royalty(self, package_id: str, body: dict) -> str:
        """Quote the royalty for this lead's tier, as a real seller would."""
        package = self.packages[package_id]
        match = next((l for l in self.leads[package_id] if l["lead_id"] == body.get("lead_id")), None)
        tier = contact_depth.tier_of(contact_depth.parse_history(match["contact_history"])) if match else ""
        return package.get("royalty_by_tier", {}).get(tier, package["royalty_atomic"])

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler), follow_redirects=False)


class FakePayer:
    def __init__(self, configured: bool = True):
        self.configured = configured
        self.quotes = []

    def is_configured(self) -> bool:
        return self.configured

    def payment_headers(self, quote) -> dict[str, str]:
        self.quotes.append(quote)
        return {"PAYMENT-SIGNATURE": "fake-signature"}
