"""
SQLite database layer for agency-os.

All persistence logic lives here. The rest of the system works with
dataclasses from core/models.py; this module translates between
dataclasses and SQL rows.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

from core.models import Prospect, Outreach, EmailLog, SendResult, EnrichmentResult


SCHEMA = """
CREATE TABLE IF NOT EXISTS prospects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    ein TEXT,
    ntee_code TEXT,
    website_url TEXT,
    address TEXT,
    city TEXT,
    state TEXT,
    zip TEXT,
    county TEXT,
    focus_area TEXT,
    annual_revenue INTEGER,
    voter_engagement INTEGER DEFAULT 0,
    source TEXT,
    source_url TEXT,
    metadata TEXT DEFAULT '{}',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(ein)
);

CREATE TABLE IF NOT EXISTS campaigns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    config_path TEXT,
    is_active INTEGER DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS outreach (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    prospect_id INTEGER NOT NULL REFERENCES prospects(id) ON DELETE CASCADE,
    campaign_id INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    stage TEXT DEFAULT 'cold',
    touch_count INTEGER DEFAULT 0,
    contact_name TEXT,
    contact_email TEXT,
    contact_phone TEXT,
    contact_title TEXT,
    script_variant TEXT,
    last_contacted_at TIMESTAMP,
    next_follow_up_at TIMESTAMP,
    demo_link TEXT,
    notes TEXT,
    activity_log TEXT DEFAULT '[]',
    assigned_to TEXT,
    closed_at TIMESTAMP,
    close_reason TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(prospect_id, campaign_id)
);

CREATE TABLE IF NOT EXISTS email_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    outreach_id INTEGER NOT NULL REFERENCES outreach(id) ON DELETE CASCADE,
    campaign_id INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    template_key TEXT,
    subject TEXT,
    body TEXT,
    status TEXT,
    provider_message_id TEXT,
    sent_at TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_outreach_stage ON outreach(stage);
CREATE INDEX IF NOT EXISTS idx_outreach_next_follow_up ON outreach(next_follow_up_at);
CREATE INDEX IF NOT EXISTS idx_outreach_campaign ON outreach(campaign_id);
CREATE INDEX IF NOT EXISTS idx_prospects_ein ON prospects(ein);
CREATE INDEX IF NOT EXISTS idx_prospects_county ON prospects(county);
"""


class Database:
    """Thin SQLite wrapper with dataclass (de)serialization."""

    def __init__(self, db_path: str = "db.sqlite"):
        self.db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self.db_path)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)
            self._conn.commit()
        return self._conn

    # ── Prospects ──────────────────────────────────────────────────────

    def upsert_prospect(self, p: Prospect) -> int:
        """Insert or update a prospect by EIN (or name+state if no EIN)."""
        c = self.conn
        if p.ein:
            row = c.execute("SELECT id FROM prospects WHERE ein = ?", (p.ein,)).fetchone()
        else:
            row = c.execute(
                "SELECT id FROM prospects WHERE name = ? AND state = ?",
                (p.name, p.state),
            ).fetchone()

        if row:
            c.execute(
                """UPDATE prospects SET name=?, ein=?, ntee_code=?, website_url=?,
                   address=?, city=?, state=?, zip=?, county=?, focus_area=?,
                   annual_revenue=?, voter_engagement=?, source=?, source_url=?,
                   metadata=?, updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (
                    p.name, p.ein, p.ntee_code, p.website_url,
                    p.address, p.city, p.state, p.zip, p.county, p.focus_area,
                    p.annual_revenue, int(p.voter_engagement), p.source, p.source_url,
                    json.dumps(p.metadata), row["id"],
                ),
            )
            c.commit()
            return row["id"]

        cur = c.execute(
            """INSERT INTO prospects (name, ein, ntee_code, website_url, address,
               city, state, zip, county, focus_area, annual_revenue,
               voter_engagement, source, source_url, metadata)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                p.name, p.ein, p.ntee_code, p.website_url, p.address,
                p.city, p.state, p.zip, p.county, p.focus_area,
                p.annual_revenue, int(p.voter_engagement), p.source, p.source_url,
                json.dumps(p.metadata),
            ),
        )
        c.commit()
        return cur.lastrowid

    def get_prospect(self, prospect_id: int) -> Optional[Prospect]:
        row = self.conn.execute("SELECT * FROM prospects WHERE id = ?", (prospect_id,)).fetchone()
        return self._row_to_prospect(row) if row else None

    def _row_to_prospect(self, row: sqlite3.Row) -> Prospect:
        return Prospect(
            id=row["id"],
            name=row["name"],
            ein=row["ein"],
            ntee_code=row["ntee_code"],
            website_url=row["website_url"],
            address=row["address"],
            city=row["city"],
            state=row["state"],
            zip=row["zip"],
            county=row["county"],
            focus_area=row["focus_area"],
            annual_revenue=row["annual_revenue"],
            voter_engagement=bool(row["voter_engagement"]),
            source=row["source"],
            source_url=row["source_url"],
            metadata=json.loads(row["metadata"] or "{}"),
            created_at=datetime.fromisoformat(row["created_at"]) if row["created_at"] else None,
            updated_at=datetime.fromisoformat(row["updated_at"]) if row["updated_at"] else None,
        )

    # ── Campaigns ──────────────────────────────────────────────────────

    def upsert_campaign(self, name: str, config_path: str) -> int:
        c = self.conn
        row = c.execute("SELECT id FROM campaigns WHERE name = ?", (name,)).fetchone()
        if row:
            c.execute("UPDATE campaigns SET config_path=? WHERE id=?", (config_path, row["id"]))
            c.commit()
            return row["id"]
        cur = c.execute(
            "INSERT INTO campaigns (name, config_path) VALUES (?, ?)", (name, config_path)
        )
        c.commit()
        return cur.lastrowid

    def get_campaign_id(self, name: str) -> Optional[int]:
        row = self.conn.execute("SELECT id FROM campaigns WHERE name = ?", (name,)).fetchone()
        return row["id"] if row else None

    # ── Outreach ───────────────────────────────────────────────────────

    def upsert_outreach(self, prospect_id: int, campaign_id: int) -> int:
        """Create an outreach row if it doesn't exist (idempotent)."""
        c = self.conn
        row = c.execute(
            "SELECT id FROM outreach WHERE prospect_id = ? AND campaign_id = ?",
            (prospect_id, campaign_id),
        ).fetchone()
        if row:
            return row["id"]
        cur = c.execute(
            """INSERT INTO outreach (prospect_id, campaign_id, stage, touch_count)
               VALUES (?, ?, 'cold', 0)""",
            (prospect_id, campaign_id),
        )
        c.commit()
        return cur.lastrowid

    def get_outreach(self, outreach_id: int) -> Optional[Outreach]:
        row = self.conn.execute("SELECT * FROM outreach WHERE id = ?", (outreach_id,)).fetchone()
        return self._row_to_outreach(row) if row else None

    def get_due_outreach(self, campaign_name: str, limit: int = 50) -> list[Outreach]:
        """Get outreach rows that are due for a follow-up touch."""
        c = self.conn
        rows = c.execute(
            """SELECT o.* FROM outreach o
               JOIN campaigns c ON o.campaign_id = c.id
               WHERE c.name = ?
                 AND o.stage NOT IN ('closed_won', 'closed_lost', 'nurture')
                 AND (o.next_follow_up_at IS NULL OR o.next_follow_up_at <= CURRENT_TIMESTAMP)
               ORDER BY COALESCE(o.next_follow_up_at, '1970-01-01') ASC
               LIMIT ?""",
            (campaign_name, limit),
        ).fetchall()
        return [self._row_to_outreach(r) for r in rows]

    def update_outreach(self, outreach_id: int, updates: dict) -> None:
        c = self.conn
        allowed = [
            "stage", "touch_count", "contact_name", "contact_email", "contact_phone",
            "contact_title", "script_variant", "last_contacted_at", "next_follow_up_at",
            "demo_link", "notes", "activity_log", "assigned_to", "closed_at", "close_reason",
        ]
        sets = []
        vals = []
        for k, v in updates.items():
            if k not in allowed:
                continue
            if k == "activity_log" and isinstance(v, list):
                v = json.dumps(v)
            sets.append(f"{k} = ?")
            vals.append(v)
        sets.append("updated_at = CURRENT_TIMESTAMP")
        vals.append(outreach_id)
        c.execute(f"UPDATE outreach SET {', '.join(sets)} WHERE id = ?", vals)
        c.commit()

    def get_all_outreach_by_stage(self, campaign_name: str) -> dict[str, int]:
        c = self.conn
        rows = c.execute(
            """SELECT o.stage, COUNT(*) as cnt FROM outreach o
               JOIN campaigns c ON o.campaign_id = c.id
               WHERE c.name = ? GROUP BY o.stage""",
            (campaign_name,),
        ).fetchall()
        return {r["stage"]: r["cnt"] for r in rows}

    def move_stale_to_nurture(self, campaign_name: str, stale_days: int) -> int:
        """Move prospects with no contact in N days to nurture. Returns count moved."""
        c = self.conn
        cur = c.execute(
            """UPDATE outreach SET stage = 'nurture', updated_at = CURRENT_TIMESTAMP
               WHERE id IN (
                 SELECT o.id FROM outreach o
                 JOIN campaigns c ON o.campaign_id = c.id
                 WHERE c.name = ?
                   AND o.stage NOT IN ('closed_won', 'closed_lost', 'nurture')
                   AND o.last_contacted_at IS NOT NULL
                   AND o.last_contacted_at < datetime('now', ?)
               )""",
            (campaign_name, f"-{stale_days} days"),
        )
        c.commit()
        return cur.rowcount

    def _row_to_outreach(self, row: sqlite3.Row) -> Outreach:
        return Outreach(
            id=row["id"],
            prospect_id=row["prospect_id"],
            campaign_id=row["campaign_id"],
            stage=row["stage"],
            touch_count=row["touch_count"],
            contact_name=row["contact_name"],
            contact_email=row["contact_email"],
            contact_phone=row["contact_phone"],
            contact_title=row["contact_title"],
            script_variant=row["script_variant"],
            last_contacted_at=datetime.fromisoformat(row["last_contacted_at"]) if row["last_contacted_at"] else None,
            next_follow_up_at=datetime.fromisoformat(row["next_follow_up_at"]) if row["next_follow_up_at"] else None,
            demo_link=row["demo_link"],
            notes=row["notes"],
            activity_log=json.loads(row["activity_log"] or "[]"),
            assigned_to=row["assigned_to"],
            closed_at=datetime.fromisoformat(row["closed_at"]) if row["closed_at"] else None,
            close_reason=row["close_reason"],
            created_at=datetime.fromisoformat(row["created_at"]) if row["created_at"] else None,
            updated_at=datetime.fromisoformat(row["updated_at"]) if row["updated_at"] else None,
        )

    # ── Email Log ──────────────────────────────────────────────────────

    def log_email(
        self, outreach_id: int, campaign_id: int, template_key: str,
        subject: str, body: str, result: SendResult,
    ) -> int:
        cur = self.conn.execute(
            """INSERT INTO email_log (outreach_id, campaign_id, template_key,
               subject, body, status, provider_message_id, sent_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                outreach_id, campaign_id, template_key, subject, body,
                result.status, result.provider_message_id,
                (result.sent_at or datetime.now()).isoformat(),
            ),
        )
        self.conn.commit()
        return cur.lastrowid

    def get_pipeline_stats(self, campaign_name: str) -> dict:
        c = self.conn
        stage_counts = self.get_all_outreach_by_stage(campaign_name)
        total_prospects = c.execute(
            """SELECT COUNT(*) FROM outreach o
               JOIN campaigns c ON o.campaign_id = c.id WHERE c.name = ?""",
            (campaign_name,),
        ).fetchone()[0]
        total_emails = c.execute(
            """SELECT COUNT(*) FROM email_log e
               JOIN campaigns c ON e.campaign_id = c.id WHERE c.name = ?""",
            (campaign_name,),
        ).fetchone()[0]
        opened = c.execute(
            """SELECT COUNT(*) FROM email_log e
               JOIN campaigns c ON e.campaign_id = c.id
               WHERE c.name = ? AND e.status = 'opened'""",
            (campaign_name,),
        ).fetchone()[0]
        replied = c.execute(
            """SELECT COUNT(*) FROM email_log e
               JOIN campaigns c ON e.campaign_id = c.id
               WHERE c.name = ? AND e.status = 'replied'""",
            (campaign_name,),
        ).fetchone()[0]
        return {
            "campaign": campaign_name,
            "total_prospects": total_prospects,
            "total_emails_sent": total_emails,
            "stage_counts": stage_counts,
            "open_rate": f"{(opened / total_emails * 100):.1f}%" if total_emails else "0%",
            "reply_rate": f"{(replied / total_emails * 100):.1f}%" if total_emails else "0%",
        }

    # ── Enrichment ─────────────────────────────────────────────────────

    def apply_enrichment(self, outreach_id: int, result: EnrichmentResult, prospect_id: int = None) -> None:
        c = self.conn
        updates = {}
        if result.contact_name:
            updates["contact_name"] = result.contact_name
        if result.contact_email:
            updates["contact_email"] = result.contact_email
        if result.contact_phone:
            updates["contact_phone"] = result.contact_phone
        if result.contact_title:
            updates["contact_title"] = result.contact_title
        if updates:
            self.update_outreach(outreach_id, updates)

        # If the enricher discovered a website, save it back to the prospect
        if prospect_id and result.raw.get("website"):
            existing = c.execute(
                "SELECT website_url FROM prospects WHERE id = ?", (prospect_id,)
            ).fetchone()
            if existing and not existing["website_url"]:
                c.execute(
                    "UPDATE prospects SET website_url = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (result.raw["website"], prospect_id),
                )
                c.commit()