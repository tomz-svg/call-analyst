"""
Call Analyst: Fathom webhook -> Claude -> Slack

1. Fathom posts a finished meeting to /fathom
2. Claude turns the transcript into a structured recap
3. Recap is posted to the sales channel (with upsell/churn flags)
4. Each action item owner gets a Slack DM; open items are re-chased via /chase
5. A draft follow-up for quiet leads is posted in the recap thread
"""
import base64
import hashlib
import hmac
import json
import os
import sqlite3
import threading
import time

import anthropic
from flask import Flask, abort, jsonify, request
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

SLACK_CHANNEL = os.environ["SLACK_CHANNEL_ID"]            # e.g. C0123456789 (#sales)
ALERT_USER = os.environ.get("SLACK_ALERT_USER_ID")        # who gets upsell/churn pings
FATHOM_SECRET = os.environ.get("FATHOM_WEBHOOK_SECRET")   # whsec_... (recommended)
CHASE_TOKEN = os.environ.get("CHASE_TOKEN", "")           # protects /chase
MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")
CHASE_AFTER_HOURS = int(os.environ.get("CHASE_AFTER_HOURS", "24"))
DB_PATH = os.environ.get("DB_PATH", "call_analyst.db")

app = Flask(__name__)
slack = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
claude = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
db_lock = threading.Lock()


# ---------------------------------------------------------------- storage
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS action_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                meeting_url TEXT, task TEXT, owner_email TEXT,
                slack_user_id TEXT, due TEXT,
                status TEXT DEFAULT 'open',
                last_nudged REAL DEFAULT 0, created REAL)"""
        )
        c.execute("CREATE TABLE IF NOT EXISTS seen (meeting_url TEXT PRIMARY KEY)")


init_db()


# ---------------------------------------------------------------- Fathom
def verify_signature(raw: bytes) -> bool:
    """Standard-Webhooks style signature check. Skipped if no secret is set.
    Confirm header names against Fathom's current webhook docs."""
    if not FATHOM_SECRET:
        return True
    msg_id = request.headers.get("webhook-id", "")
    ts = request.headers.get("webhook-timestamp", "")
    sigs = request.headers.get("webhook-signature", "")
    if not (msg_id and ts and sigs):
        return False
    if abs(time.time() - int(ts)) > 300:
        return False
    key = base64.b64decode(FATHOM_SECRET.split("_", 1)[-1])
    signed = f"{msg_id}.{ts}.".encode() + raw
    expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    return any(hmac.compare_digest(expected, s.split(",", 1)[-1]) for s in sigs.split())


def transcript_text(meeting: dict) -> str:
    t = meeting.get("transcript")
    if isinstance(t, str):
        return t
    lines = []
    for seg in t or []:
        speaker = seg.get("speaker", {})
        name = speaker.get("display_name", "Speaker") if isinstance(speaker, dict) else speaker
        lines.append(f"[{seg.get('timestamp', '')}] {name}: {seg.get('text', '')}")
    return "\n".join(lines)


# ---------------------------------------------------------------- Claude
SYSTEM = """You are a sales call analyst. Read the call transcript and return ONLY valid JSON
(no prose, no markdown fences) with this shape:
{
  "outcome": "short label, e.g. Opportunity Created / Needs Follow-up / Lost / Churn Risk",
  "deal_stage": "best guess or null",
  "summary": "2-3 sentences",
  "objections": ["each objection, with the specific concern the prospect voiced"],
  "next_steps": ["..."],
  "signals": [{"type": "upsell" | "churn", "evidence": "what was said", "urgency": "low|medium|high"}],
  "action_items": [{"task": "...", "owner_name": "...", "owner_email": "email or null", "due": "date text or null"}],
  "follow_up_draft": "A short, specific email to the prospect that references what THEY said on the call. No generic filler."
}
Only include signals and objections that are actually supported by the transcript. Do not invent."""


def analyze(meeting: dict) -> dict:
    invitees = meeting.get("calendar_invitees") or []
    context = f"Title: {meeting.get('title') or meeting.get('meeting_title')}\nInvitees: {json.dumps(invitees)}\n\nTranscript:\n{transcript_text(meeting)}"
    resp = claude.messages.create(
        model=MODEL,
        max_tokens=2000,
        system=SYSTEM,
        messages=[{"role": "user", "content": context[:150_000]}],
    )
    raw = "".join(b.text for b in resp.content if b.type == "text").strip()
    raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return json.loads(raw)


# ---------------------------------------------------------------- Slack
def lookup_slack_user(email):
    if not email:
        return None
    try:
        return slack.users_lookupByEmail(email=email)["user"]["id"]
    except SlackApiError:
        return None


def bullets(items):
    return "\n".join(f"• {i}" for i in items) or "_None_"


def post_recap(meeting: dict, a: dict) -> str:
    url = meeting.get("share_url") or meeting.get("url") or ""
    title = meeting.get("title") or meeting.get("meeting_title") or "Call"
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"Call Recap: {title}"[:150]}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Outcome:* {a['outcome']}"
                                     + (f"  |  *Stage:* {a['deal_stage']}" if a.get("deal_stage") else "")
                                     + f"\n{a['summary']}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Key objections*\n{bullets(a['objections'])}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Next steps*\n{bullets(a['next_steps'])}"}},
    ]
    if a.get("signals"):
        sig = "\n".join(f"{':chart_with_upwards_trend:' if s['type']=='upsell' else ':rotating_light:'} "
                        f"*{s['type'].title()}* ({s['urgency']}): {s['evidence']}" for s in a["signals"])
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*Money signals*\n{sig}"}})
    if url:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": f"<{url}|View in Fathom>"}]})

    res = slack.chat_postMessage(channel=SLACK_CHANNEL, text=f"Call recap: {title}", blocks=blocks)
    ts = res["ts"]

    if a.get("follow_up_draft"):
        slack.chat_postMessage(channel=SLACK_CHANNEL, thread_ts=ts,
                               text=f"*Draft follow-up (edit before sending):*\n>>> {a['follow_up_draft']}")
    return ts


def alert_signals(meeting, a):
    if not (ALERT_USER and a.get("signals")):
        return
    title = meeting.get("title") or "a call"
    lines = "\n".join(f"• *{s['type'].title()}* ({s['urgency']}): {s['evidence']}" for s in a["signals"])
    slack.chat_postMessage(channel=ALERT_USER, text=f":moneybag: Signals from *{title}*\n{lines}")


def dm_action_items(meeting, a):
    url = meeting.get("share_url") or meeting.get("url") or ""
    with db_lock, db() as c:
        for item in a.get("action_items", []):
            uid = lookup_slack_user(item.get("owner_email"))
            c.execute("INSERT INTO action_items (meeting_url, task, owner_email, slack_user_id, due, created, last_nudged)"
                      " VALUES (?,?,?,?,?,?,?)",
                      (url, item["task"], item.get("owner_email"), uid, item.get("due"), time.time(), time.time()))
            if uid:
                slack.chat_postMessage(
                    channel=uid,
                    text=f":clipboard: New action item from a call: *{item['task']}*"
                         + (f" (due {item['due']})" if item.get("due") else "")
                         + (f"\n<{url}|Call recording>" if url else "")
                         + "\nReply `done` in this DM when it's complete.")


# ---------------------------------------------------------------- pipeline
def process(meeting: dict):
    url = meeting.get("share_url") or meeting.get("url") or str(meeting.get("recording_id", ""))
    with db_lock, db() as c:  # idempotency: Fathom may retry
        if c.execute("SELECT 1 FROM seen WHERE meeting_url=?", (url,)).fetchone():
            return
        c.execute("INSERT INTO seen VALUES (?)", (url,))
    try:
        a = analyze(meeting)
        post_recap(meeting, a)
        alert_signals(meeting, a)
        dm_action_items(meeting, a)
    except Exception as e:  # keep the bot visible when it fails
        app.logger.exception("processing failed")
        slack.chat_postMessage(channel=SLACK_CHANNEL, text=f":warning: Call Analyst failed on {url}: {e}")


@app.post("/fathom")
def fathom_webhook():
    raw = request.get_data()
    if not verify_signature(raw):
        abort(401)
    threading.Thread(target=process, args=(request.get_json(force=True),), daemon=True).start()
    return jsonify(ok=True)  # respond fast, work in background


@app.post("/chase")
def chase():
    """Call this from cron (e.g. every morning). Nudges open items older than CHASE_AFTER_HOURS."""
    if CHASE_TOKEN and request.headers.get("X-Chase-Token") != CHASE_TOKEN:
        abort(401)
    cutoff = time.time() - CHASE_AFTER_HOURS * 3600
    nudged = 0
    with db_lock, db() as c:
        rows = c.execute("SELECT * FROM action_items WHERE status='open' AND slack_user_id IS NOT NULL"
                         " AND last_nudged < ?", (cutoff,)).fetchall()
        for r in rows:
            slack.chat_postMessage(channel=r["slack_user_id"],
                                   text=f":hourglass: Still open: *{r['task']}*"
                                        + (f" (due {r['due']})" if r["due"] else "")
                                        + "\nReply `done` when finished.")
            c.execute("UPDATE action_items SET last_nudged=? WHERE id=?", (time.time(), r["id"]))
            nudged += 1
    return jsonify(nudged=nudged)


@app.post("/slack/events")
def slack_events():
    """Optional: Slack Events API endpoint so replying `done` in a DM closes items.
    Needs the im:history scope and the message.im event subscription."""
    body = request.get_json(force=True)
    if body.get("type") == "url_verification":
        return jsonify(challenge=body["challenge"])
    ev = body.get("event", {})
    if ev.get("type") == "message" and ev.get("channel_type") == "im" and not ev.get("bot_id") \
            and ev.get("text", "").strip().lower() == "done":
        with db_lock, db() as c:
            row = c.execute("SELECT id, task FROM action_items WHERE slack_user_id=? AND status='open'"
                            " ORDER BY created LIMIT 1", (ev["user"],)).fetchone()
            if row:
                c.execute("UPDATE action_items SET status='done' WHERE id=?", (row["id"],))
                slack.chat_postMessage(channel=ev["channel"], text=f":white_check_mark: Closed: {row['task']}")
    return jsonify(ok=True)


@app.get("/health")
def health():
    return jsonify(ok=True)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 3000)))
