"""
Call tasks: people a caller should phone next, and why (docs/MAILER_FOLLOWUP_CALLS.md, M3-M5).

A cadence step on the `call` channel doesn't send anything. It opens a call
task for the outreach (core/pipeline.py), which shows on the Calls due page
(/calls/due). Logging the call (/call-log/record) closes the task and counts
the touch, so the next step's timing runs from the call, not from the task.

A step can also wait for an event with `after:` (core/campaign.py CadenceStep):

- `mail_delivered`: due `delay_days` after Lob's delivery scan for the
  outreach's latest mailer, or `fallback_days` after it was sent when no scan
  came. Returned mail makes it due at once, labeled `mail_returned`.
- `call_logged`: due `delay_days` after the call task before it was closed.
  It waits while that task is open. With no task (the step before reached
  them some other way) there's nothing to wait for.

Only people make calls. Nothing here dials or sends.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

AFTER_EVENTS = ("mail_delivered", "call_logged")
# Lob piece ids: postcards, letters, self-mailers (plugins/channels/lob_direct_mail.py)
MAIL_PIECE_PREFIXES = ("psc_", "ltr_", "sfm_")
# Call outcomes that end the outreach's sequence. log_call already closes it
# for do_not_call and for a not_interested interest level.
ENDS_SEQUENCE = {"wrong_number"}
REASONS = {
    "mail_delivered": "Mailer delivered",
    "mail_unconfirmed": "Mailer sent, delivery not confirmed",
    "mail_returned": "Mailer returned",
    "manual": "Call step",
}
CLOSED_STAGES = ("closed_won", "closed_lost", "nurture")


def _as_datetime(value) -> Optional[datetime]:
    if value is None or isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def latest_mail_piece(db, outreach_id: int):
    """The outreach's most recent mailer sent through Lob (provider_message_id, sent_at), or None."""
    like = " OR ".join("provider_message_id LIKE ?" for _ in MAIL_PIECE_PREFIXES)
    return db.conn.execute(
        f"""SELECT provider_message_id, sent_at FROM email_log
            WHERE outreach_id = ? AND status = 'sent' AND ({like})
            ORDER BY sent_at DESC NULLS LAST, id DESC LIMIT 1""",
        (outreach_id, *(p + "%" for p in MAIL_PIECE_PREFIXES))).fetchone()


def event_ready(db, step, outreach, now: Optional[datetime] = None) -> tuple[bool, str]:
    """(due now, call task reason) for a cadence step's `after:` condition."""
    now = now or datetime.now()
    if step.after == "mail_delivered":
        piece = latest_mail_piece(db, outreach.id)
        if piece is None:
            return True, "manual"  # nothing was mailed, so nothing to wait for
        events = {r["kind"]: _as_datetime(r["created_at"]) for r in db.conn.execute(
            """SELECT kind, MIN(created_at) AS created_at FROM contact_events
               WHERE kind IN ('mail_delivered', 'mail_returned') AND detail::jsonb ->> 'piece_id' = ?
               GROUP BY kind""", (piece["provider_message_id"],)).fetchall()}
        if events.get("mail_returned"):
            return True, "mail_returned"
        if events.get("mail_delivered"):
            return events["mail_delivered"] + timedelta(days=step.delay_days) <= now, "mail_delivered"
        sent_at = _as_datetime(piece["sent_at"]) or now
        return sent_at + timedelta(days=step.fallback_days) <= now, "mail_unconfirmed"
    if step.after == "call_logged":
        task = db.conn.execute(
            """SELECT done_at FROM call_tasks WHERE outreach_id = ? AND touch < ?
               ORDER BY touch DESC, id DESC LIMIT 1""", (outreach.id, step.touch)).fetchone()
        if task is None:
            return True, "manual"
        done_at = _as_datetime(task["done_at"])
        return bool(done_at and done_at + timedelta(days=step.delay_days) <= now), "manual"
    return True, "manual"


def open_task(db, *, outreach_id: int, campaign_id: int, touch: int, script_key: str,
              reason: str = "manual", due_at: Optional[datetime] = None) -> bool:
    """Open a call task for the outreach. False when one is already open (it's left as is)."""
    row = db.conn.execute(
        """INSERT INTO call_tasks (outreach_id, campaign_id, touch, script_key, reason, due_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT (outreach_id) WHERE done_at IS NULL DO NOTHING RETURNING id""",
        (outreach_id, campaign_id, touch, script_key, reason if reason in REASONS else "manual",
         (due_at or datetime.now()).isoformat())).fetchone()
    return row is not None


def open_task_for(db, outreach_id: int):
    return db.conn.execute(
        "SELECT * FROM call_tasks WHERE outreach_id = ? AND done_at IS NULL", (outreach_id,)).fetchone()


def complete(db, call, call_log_id: int, campaign=None) -> Optional[dict]:
    """Close the outreach's open call task for a logged call and count the touch.

    `call` is the CallLog just saved with db.log_call, which already moved the
    stage. `campaign` (its CampaignConfig) gives the step's delay_days for the
    next follow-up; a next-step date on the call wins over it. Returns the
    closed task, or None when the outreach had no open task (nothing changes).
    """
    task = db.conn.execute(
        """UPDATE call_tasks SET done_at = CURRENT_TIMESTAMP, call_log_id = ?
           WHERE outreach_id = ? AND done_at IS NULL RETURNING *""",
        (call_log_id, call.outreach_id)).fetchone()
    if task is None:
        return None
    outreach = db.get_outreach(call.outreach_id)
    if outreach is None:
        return dict(task)
    now = datetime.now()
    updates = {"touch_count": max(outreach.touch_count, task["touch"] + 1), "script_variant": task["script_key"]}
    step = next((s for s in (campaign.cadence if campaign else []) if s.touch == task["touch"]), None)
    if step is not None and not call.next_step_date:
        updates["next_follow_up_at"] = (now + timedelta(days=step.delay_days)).isoformat()
    if call.outcome in ENDS_SEQUENCE and outreach.stage not in CLOSED_STAGES:
        updates.update(stage="closed_lost", closed_at=now.isoformat(),
                       close_reason=call.notes or "Wrong number")
    db.update_outreach(call.outreach_id, updates)
    return dict(task)


def due(db, campaign_ids: list[int], hidden_campaigns=(), limit: int = 200) -> list[dict]:
    """Open call tasks due now in these campaigns, oldest first, for the Calls due page."""
    if not campaign_ids:
        return []
    hidden_sql, hidden_params = db.hidden_clause(hidden_campaigns, "o.prospect_id")
    marks = ",".join("?" * len(campaign_ids))
    closed = ",".join("?" * len(CLOSED_STAGES))
    rows = db.conn.execute(
        f"""SELECT t.id, t.outreach_id, t.campaign_id, t.touch, t.script_key, t.reason, t.due_at,
                   o.prospect_id, o.stage, o.contact_name, o.contact_title, o.contact_phone,
                   p.name AS prospect_name, p.city, p.state, p.do_not_call, c.name AS campaign
            FROM call_tasks t
            JOIN outreach o ON o.id = t.outreach_id
            JOIN prospects p ON p.id = o.prospect_id
            JOIN campaigns c ON c.id = t.campaign_id
            WHERE t.done_at IS NULL AND t.due_at <= CURRENT_TIMESTAMP
              AND t.campaign_id IN ({marks}) AND o.stage NOT IN ({closed})
              AND {hidden_sql}
            ORDER BY t.due_at ASC, t.id ASC LIMIT ?""",
        (*campaign_ids, *CLOSED_STAGES, *hidden_params, limit)).fetchall()
    return [dict(r) for r in rows]
