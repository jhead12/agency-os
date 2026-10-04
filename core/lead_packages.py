"""
x402 lead packages: browse providers' catalogs, unlock a package into a
campaign (pay once, import its leads), and pay a royalty the first time a
package lead is contacted.

Provider contract (each provider is a base URL):

    GET  {base}/packages                       free catalog: {"packages": [...]}
    GET  {base}/packages/{id}/leads            x402: unlock, returns {"leads": [...]}
    POST {base}/packages/{id}/contacts         x402: royalty for {"lead_id", "campaign_ref"}

Catalog entries carry prices in USDC atomic units (6 decimals) plus the
pay-to address and CAIP-2 network; quotes that differ from the catalog are
refused (core/payments.py).

Security:

- Providers come only from AGENCY_OS_LEAD_PROVIDERS (comma-separated, set by
  whoever deploys the app). https only, except localhost for development.
  Requests never follow redirects, and package ids are validated, so a
  provider can't steer requests to other hosts.
- Responses are size- and count-limited, and every lead field is
  whitelisted, type-checked and truncated before it touches the database.
- An existing prospect is never overwritten by a package lead; it's counted
  as a duplicate instead.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import quote, urlsplit

import httpx

from core import contact_depth
from core.access import CurrentUser
from core.db import Database
from core.models import Prospect
from core.payments import (
    Expected, NETWORKS, PaidResult, Payer, SpendPolicy, atomic_to_usd, default_payer, paid_request,
)

MAX_RESPONSE_BYTES = 25 * 1024 * 1024
MAX_LEADS_PER_PACKAGE = 50_000
MAX_CATALOG_PACKAGES = 500
GUARANTEE_MIN_RATE = 0.9
_TIMEOUT = httpx.Timeout(20.0, connect=5.0)
_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,63}$")
_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

# ── Providers ──────────────────────────────────────────────────────────


def normalize_provider(url: str) -> Optional[str]:
    """A safe provider base URL, or None. https, or http on localhost only."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    if parts.username or parts.password or parts.query or parts.fragment or not parts.hostname:
        return None
    if parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in _LOCAL_HOSTS):
        return f"{parts.scheme}://{parts.netloc}{parts.path.rstrip('/')}"
    return None


def configured_providers() -> list[str]:
    raw = os.environ.get("AGENCY_OS_LEAD_PROVIDERS", "")
    providers = []
    for item in raw.split(","):
        base = normalize_provider(item) if item.strip() else None
        if base and base not in providers:
            providers.append(base)
    return providers


def make_http(transport: Optional[httpx.BaseTransport] = None) -> httpx.Client:
    return httpx.Client(
        timeout=_TIMEOUT, follow_redirects=False, transport=transport,
        headers={"User-Agent": "agency-os lead-packages"},
    )


def _package_url(provider: str, package_id: str, tail: str) -> str:
    return f"{provider}/packages/{quote(package_id, safe='')}/{tail}"


def _json_body(response: httpx.Response) -> Any:
    if len(response.content) > MAX_RESPONSE_BYTES:
        raise ValueError("Response too large")
    return response.json()


# ── Catalog ────────────────────────────────────────────────────────────


def _text(value: Any, limit: int = 300) -> Optional[str]:
    if value is None or isinstance(value, (dict, list)):
        return None
    text = str(value).strip()
    return text[:limit] or None


def _atomic(value: Any) -> int:
    try:
        n = int(str(value))
    except (TypeError, ValueError):
        return -1
    return n


def _tier_map(value: Any, convert) -> Optional[dict]:
    """{tier: non-negative int} for known tiers; {} if absent, None if malformed."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        return None
    out = {}
    for tier, raw in value.items():
        number = convert(raw)
        if tier not in contact_depth.RANK or number < 0:
            return None
        out[tier] = number
    return out


@dataclass(frozen=True)
class Package:
    provider: str
    package_id: str
    title: str
    unlock_price_atomic: int
    royalty_atomic: int
    pay_to: str
    network: str
    industry: str = ""
    region: str = ""
    lead_count: int = 0
    sourcing: str = ""
    consent_note: str = ""
    sms_consent: bool = False
    updated_at: str = ""
    guarantee: dict = field(default_factory=dict)
    royalty_by_tier: dict = field(default_factory=dict)   # tier -> atomic
    tier_mix: dict = field(default_factory=dict)          # tier -> lead count

    @property
    def guaranteed(self) -> bool:
        """At least 90% of leads promised at a stated contact tier."""
        try:
            rate = float(self.guarantee.get("verified_rate_min", 0))
        except (TypeError, ValueError):
            return False
        return rate >= GUARANTEE_MIN_RATE and self.guarantee_tier != "unworked"

    @property
    def guarantee_tier(self) -> str:
        tier = self.guarantee.get("tier")
        return tier if tier in contact_depth.RANK else "unworked"

    def royalty_for(self, tier: str) -> int:
        """A lead's royalty: the price for its tier, else the package default."""
        return self.royalty_by_tier.get(tier, self.royalty_atomic)

    @property
    def max_royalty_atomic(self) -> int:
        return max([self.royalty_atomic, *self.royalty_by_tier.values()])

    @property
    def unlock_usd(self) -> float:
        return atomic_to_usd(self.unlock_price_atomic)

    @property
    def royalty_usd(self) -> float:
        return atomic_to_usd(self.royalty_atomic)

    @classmethod
    def from_catalog(cls, provider: str, item: Any) -> Optional["Package"]:
        """Validate one catalog entry; None if it's unusable."""
        if not isinstance(item, dict):
            return None
        package_id = str(item.get("id", ""))
        pay_to = str(item.get("pay_to", ""))
        network = str(item.get("network", ""))
        unlock = _atomic(item.get("unlock_price_atomic"))
        royalty = _atomic(item.get("royalty_atomic"))
        if not _ID_RE.match(package_id) or not _ADDRESS_RE.match(pay_to):
            return None
        if network not in NETWORKS.values() or unlock < 0 or royalty < 0:
            return None
        guarantee = item.get("guarantee") if isinstance(item.get("guarantee"), dict) else {}
        try:
            lead_count = max(0, int(item.get("lead_count") or 0))
        except (TypeError, ValueError):
            lead_count = 0
        royalty_by_tier = _tier_map(item.get("royalty_by_tier"), _atomic)
        if royalty_by_tier is None:
            return None  # a malformed price list is not something to guess at
        tier_mix = _tier_map(item.get("tier_mix"), _atomic) or {}
        return cls(
            provider=provider, package_id=package_id,
            title=_text(item.get("title"), 200) or package_id,
            unlock_price_atomic=unlock, royalty_atomic=royalty, pay_to=pay_to, network=network,
            industry=_text(item.get("industry"), 100) or "", region=_text(item.get("region"), 100) or "",
            lead_count=lead_count, sourcing=_text(item.get("sourcing"), 500) or "",
            consent_note=_text(item.get("consent_note"), 500) or "",
            sms_consent=item.get("sms_consent") is True, updated_at=_text(item.get("updated_at"), 40) or "",
            guarantee={k: guarantee[k] for k in ("verified_rate_min", "window_days", "tier") if k in guarantee},
            royalty_by_tier=royalty_by_tier, tier_mix=tier_mix,
        )


def fetch_catalog(provider: str, http: Optional[httpx.Client] = None) -> tuple[list[Package], str]:
    """A provider's packages and an error message ("" on success). Never raises."""
    if provider not in configured_providers():
        return [], "Provider is not on the allowlist"
    client = http or make_http()
    try:
        response = client.get(f"{provider}/packages")
        if response.status_code != 200:
            return [], f"Catalog returned {response.status_code}"
        data = _json_body(response)
    except (httpx.HTTPError, ValueError) as exc:
        return [], f"Catalog unavailable: {exc}"
    finally:
        if http is None:
            client.close()
    items = data.get("packages") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return [], "Catalog is not a list of packages"
    packages = [p for p in (Package.from_catalog(provider, i) for i in items[:MAX_CATALOG_PACKAGES]) if p]
    return packages, ""


_catalog_cache: dict[str, tuple[float, list[Package], str]] = {}
CATALOG_TTL_SECONDS = 60


def cached_catalog(provider: str) -> tuple[list[Package], str]:
    """fetch_catalog, cached briefly so page views don't hammer providers.

    Only for browsing; unlocking always re-reads the catalog for current prices.
    """
    import time

    hit = _catalog_cache.get(provider)
    if hit and time.monotonic() - hit[0] < CATALOG_TTL_SECONDS:
        return hit[1], hit[2]
    packages, error = fetch_catalog(provider)
    _catalog_cache[provider] = (time.monotonic(), packages, error)
    return packages, error


def find_package(provider: str, package_id: str, http: Optional[httpx.Client] = None) -> tuple[Optional[Package], str]:
    packages, error = fetch_catalog(provider, http)
    if error:
        return None, error
    match = next((p for p in packages if p.package_id == package_id), None)
    return (match, "") if match else (None, "Package not found in the provider's catalog")


# ── Leads ──────────────────────────────────────────────────────────────

_PROSPECT_FIELDS = ("ein", "ntee_code", "address", "city", "state", "zip", "county", "focus_area")


def parse_lead(item: Any) -> Optional[dict]:
    """Whitelist and clean one lead; None if it has no id or name."""
    if not isinstance(item, dict):
        return None
    lead_id = _text(item.get("lead_id"), 128)
    name = _text(item.get("name"), 200)
    if not lead_id or not name or not _ID_RE.match(lead_id):
        return None
    lead = {"lead_id": lead_id, "name": name}
    for key in _PROSPECT_FIELDS:
        lead[key] = _text(item.get(key), 200)
    if lead["state"]:
        lead["state"] = lead["state"][:2].upper()
    website = _text(item.get("website_url"), 300)
    lead["website_url"] = website if website and website.startswith(("https://", "http://")) else None
    email = _text(item.get("contact_email"), 254)
    lead["contact_email"] = email.lower() if email and _EMAIL_RE.match(email) else None
    lead["contact_name"] = _text(item.get("contact_name"), 120)
    lead["contact_title"] = _text(item.get("contact_title"), 120)
    phone = _text(item.get("contact_phone"), 40)
    lead["contact_phone"] = phone if phone and re.fullmatch(r"[0-9+()\-. ]{7,40}", phone) else None
    # The tier comes from the history we can read, not from what the seller claims.
    lead["history"] = contact_depth.parse_history(item.get("contact_history"))
    lead["tier"] = contact_depth.tier_of(lead["history"])
    claimed = item.get("tier")
    lead["claimed_tier"] = claimed if claimed in contact_depth.RANK else None
    return lead


@dataclass
class UnlockResult:
    ok: bool
    message: str
    lead_package_id: Optional[int] = None
    imported: int = 0
    duplicates: int = 0
    payment: Optional[PaidResult] = None


def _read_leads(response: httpx.Response) -> tuple[list[dict], str]:
    """Clean leads from an unlock response, and an error if it was unreadable."""
    try:
        data = _json_body(response)
    except ValueError as exc:
        return [], f"Paid, but the leads could not be read: {exc}"
    raw_leads = data.get("leads") if isinstance(data, dict) else data
    if not isinstance(raw_leads, list):
        return [], "Paid, but the response had no list of leads"
    return [l for l in (parse_lead(i) for i in raw_leads[:MAX_LEADS_PER_PACKAGE]) if l], ""


def _import_leads(db: Database, package: Package, campaign_id: int, user: Optional[CurrentUser],
                  payment: PaidResult, leads: list[dict]) -> tuple[int, int, int]:
    """Record the package and add its new leads to the campaign, in one transaction.

    Returns (lead_package_id, imported, duplicates). The package row is
    written even with no leads, so a paid unlock is always traceable.
    """
    imported = duplicates = 0
    with db.conn.raw.transaction():
        c = db.conn
        lp_id = c.execute(
            """INSERT INTO lead_packages (provider, package_id, campaign_id, title, industry, region,
                   lead_count, unlock_price_atomic, royalty_atomic, royalty_by_tier, pay_to, network,
                   guarantee, guarantee_tier, consent_note, sms_consent, unlocked_by, tx_hash)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id""",
            (package.provider, package.package_id, campaign_id, package.title, package.industry,
             package.region, len(leads), package.unlock_price_atomic, package.royalty_atomic,
             json.dumps(package.royalty_by_tier), package.pay_to, package.network,
             json.dumps(package.guarantee), package.guarantee_tier, package.consent_note,
             int(package.sms_consent), user.id if user else None, payment.tx_hash or None),
        ).fetchone()["id"]
        if payment.spend_id:
            c.execute("UPDATE spend SET lead_package_id = ? WHERE id = ?", (lp_id, payment.spend_id))
        source = f"x402:{urlsplit(package.provider).netloc}"
        for lead in leads:
            if db.find_prospect_id(lead["ein"], lead["name"], lead["state"]):
                duplicates += 1  # never overwrite a prospect we already have
                continue
            prospect_id = db.upsert_prospect(Prospect(
                name=lead["name"], source=source, source_url=package.provider,
                website_url=lead["website_url"], **{k: lead[k] for k in _PROSPECT_FIELDS},
                metadata={"lead_package": {
                    "lead_package_id": lp_id, "provider": package.provider,
                    "package_id": package.package_id, "lead_id": lead["lead_id"],
                    "tier": lead["tier"], "claimed_tier": lead["claimed_tier"],
                    "royalty_atomic": package.royalty_for(lead["tier"]),
                    "history": lead["history"],
                }},
            ))
            outreach_id = db.upsert_outreach(prospect_id, campaign_id)
            contact = {k: lead[k] for k in ("contact_name", "contact_email", "contact_phone", "contact_title") if lead[k]}
            if contact:
                db.update_outreach(outreach_id, contact)
            imported += 1
        c.execute("UPDATE lead_packages SET imported_count = ?, duplicate_count = ? WHERE id = ?",
                  (imported, duplicates, lp_id))
        db._audit(c, user, "lead_package.unlock", "lead_package", lp_id, {
            "provider": package.provider, "package_id": package.package_id, "campaign_id": campaign_id,
            "amount_atomic": payment.amount_atomic, "tx_hash": payment.tx_hash,
            "imported": imported, "duplicates": duplicates,
        })
    return lp_id, imported, duplicates


def unlock(db: Database, campaign, campaign_id: int, user: Optional[CurrentUser], package: Package, *,
           http: Optional[httpx.Client] = None, payer: Optional[Payer] = None) -> UnlockResult:
    """Pay for a package and import its leads into the campaign. Never raises."""
    policy = SpendPolicy.from_config(campaign.lead_packages)
    if package.network != policy.caip:
        return UnlockResult(False, f"Package is on {package.network}, the campaign pays on {policy.caip}")
    if not package.guaranteed and not campaign.lead_packages.get("allow_unguaranteed"):
        return UnlockResult(False, "Package has no 90% verification guarantee")
    existing = db.find_lead_package(package.provider, package.package_id, campaign_id)
    if existing:
        return UnlockResult(True, "Already unlocked for this campaign", existing["id"],
                            existing["imported_count"], existing["duplicate_count"])

    client = http or make_http()
    try:
        payment = paid_request(
            db, client, payer or default_payer(), policy,
            kind="unlock", ref=f"{package.provider}/{package.package_id}",
            campaign_id=campaign_id, user_id=user.id if user else None,
            method="GET", url=_package_url(package.provider, package.package_id, "leads"),
            expected=Expected(package.pay_to, package.unlock_price_atomic),
        )
    finally:
        if http is None:
            client.close()
    if not payment.ok:
        return UnlockResult(False, payment.error or "Payment did not go through", payment=payment)

    leads, read_error = _read_leads(payment.response)
    lp_id, imported, duplicates = _import_leads(db, package, campaign_id, user, payment, leads)
    if read_error:
        return UnlockResult(False, read_error, lp_id, payment=payment)
    return UnlockResult(True, f"Unlocked: {imported} leads imported, {duplicates} already in agency-os",
                        lp_id, imported, duplicates, payment)


# ── Royalties (called by Pipeline.enqueue_outreach) ────────────────────


@dataclass
class LeadGate:
    send: bool
    reason: str = ""
    sms_allowed: bool = True
    payment: Optional[PaidResult] = None


def package_info(prospect: Prospect) -> Optional[dict]:
    info = (prospect.metadata or {}).get("lead_package")
    return info if isinstance(info, dict) and info.get("lead_package_id") else None


def _lead_royalty(info: dict, lp: dict) -> int:
    """The royalty fixed for this lead at unlock (its tier's price), else the package default."""
    try:
        return max(0, int(info.get("royalty_atomic", lp["royalty_atomic"])))
    except (TypeError, ValueError):
        return int(lp["royalty_atomic"])


def gate_contact(db: Database, campaign, campaign_id: int, prospect: Prospect, touch_count: int, *,
                 dry_run: bool = False, http: Optional[httpx.Client] = None,
                 payer: Optional[Payer] = None) -> LeadGate:
    """Whether a prospect may be contacted now, paying the royalty on first touch.

    Prospects that didn't come from a package always pass. Package leads are
    only contacted in the campaign they were unlocked into, and the first
    touch happens only after the royalty has settled.
    """
    info = package_info(prospect)
    if info is None:
        return LeadGate(True)
    lp = db.get_lead_package(int(info["lead_package_id"]))
    if lp is None or lp["campaign_id"] != campaign_id:
        return LeadGate(False, "Package lead outside the campaign it was unlocked into")
    sms_ok = bool(lp["sms_consent"])
    ref = f"{lp['provider']}/{lp['package_id']}/{info.get('lead_id', '')}"
    royalty = _lead_royalty(info, lp)
    if touch_count > 0 or royalty == 0 or db.royalty_settled(campaign_id, ref):
        return LeadGate(True, sms_allowed=sms_ok)
    if lp["provider"] not in configured_providers():
        return LeadGate(False, "Package provider is no longer on the allowlist", sms_ok)
    if dry_run:
        return LeadGate(True, f"Would pay a ${atomic_to_usd(royalty):.2f} royalty", sms_ok)

    policy = SpendPolicy.from_config(campaign.lead_packages)
    client = http or make_http()
    try:
        result = paid_request(
            db, client, payer or default_payer(), policy,
            kind="royalty", ref=ref, campaign_id=campaign_id, user_id=lp["unlocked_by"],
            method="POST", url=_package_url(lp["provider"], lp["package_id"], "contacts"),
            json_body={"lead_id": info.get("lead_id"), "campaign_ref": f"c{campaign_id}"},
            expected=Expected(lp["pay_to"], royalty),
            prospect_id=prospect.id, lead_package_id=lp["id"],
        )
    finally:
        if http is None:
            client.close()
    if result.status == "settled" or (result.status == "free"):
        return LeadGate(True, sms_allowed=sms_ok, payment=result)
    return LeadGate(False, f"Royalty not paid: {result.error}", sms_ok, result)
