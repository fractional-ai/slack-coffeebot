"""Walk recent coffee-pair messages and reply in-thread with a suggested slot."""

import argparse
import os
import re
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

load_dotenv()

SLOT_MINUTES = 30
LOOKAHEAD_DAYS = 2
LOOKBACK_MINUTES = 200
PAIR_MENTION_RE = re.compile(r"<@([UW][A-Z0-9]+)>")
# Ideal coffee windows as ((start_hour, start_minute), (end_hour, end_minute)) in local TIMEZONE.
COFFEE_WINDOWS = [
    ((9, 30), (11, 0)),
    ((13, 30), (16, 30)),
]
EVENT_TITLE = "Coffee chat!"
EVENT_DETAILS = "Auto-suggested coffee slot."
TIMEZONE = "America/New_York"
SCOPES = ["https://www.googleapis.com/auth/calendar.freebusy"]
TOKEN_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "token.json")


class CalendarAuthProvider(ABC):
    """Pluggable auth source for the Google Calendar client."""

    @abstractmethod
    def get_credentials(self):
        ...


class TokenFileAuthProvider(CalendarAuthProvider):
    """Loads (and refreshes) OAuth credentials from a local token.json file."""

    def __init__(self, token_path=TOKEN_PATH, scopes=SCOPES):
        self.token_path = token_path
        self.scopes = scopes

    def get_credentials(self):
        creds = Credentials.from_authorized_user_file(self.token_path, self.scopes)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            with open(self.token_path, "w") as f:
                f.write(creds.to_json())
        return creds


def build_calendar_service(auth_provider: CalendarAuthProvider):
    return build("calendar", "v3", credentials=auth_provider.get_credentials())


def fetch_busy_intervals(service, calendars, time_min, time_max, tz):
    body = {
        "timeMin": time_min.isoformat(),
        "timeMax": time_max.isoformat(),
        "timeZone": tz,
        "items": [{"id": c} for c in calendars],
    }
    response = service.freebusy().query(body=body).execute()
    busy_by_calendar = {}
    for cal_id, info in response["calendars"].items():
        if info.get("errors"):
            raise RuntimeError(f"Calendar {cal_id} errors: {info['errors']}")
        busy_by_calendar[cal_id] = [
            (datetime.fromisoformat(b["start"]), datetime.fromisoformat(b["end"]))
            for b in info.get("busy", [])
        ]
    return busy_by_calendar


def overlaps_any(start, end, intervals):
    return any(s < end and start < e for s, e in intervals)


def _next_quarter_hour(dt):
    minute = (dt.minute // 15 + 1) * 15 if dt.minute % 15 or dt.second or dt.microsecond else dt.minute
    extra_hours, minute = divmod(minute, 60)
    return (dt.replace(minute=0, second=0, microsecond=0)
            + timedelta(hours=extra_hours, minutes=minute))


def _window_index(start_minutes, end_minutes, windows):
    for i, ((sh, sm), (eh, em)) in enumerate(windows):
        if start_minutes >= sh * 60 + sm and end_minutes <= eh * 60 + em:
            return i
    return None


def find_common_slots(busy_by_calendar, start, end, slot_minutes, windows, tz):
    all_busy = [iv for ivs in busy_by_calendar.values() for iv in ivs]
    delta = timedelta(minutes=slot_minutes)
    step = timedelta(minutes=15)
    zone = ZoneInfo(tz)
    best_by_slot = {}
    cursor = _next_quarter_hour(start)
    while cursor + delta <= end:
        local_start = cursor.astimezone(zone)
        local_end = (cursor + delta).astimezone(zone)
        start_minutes = local_start.hour * 60 + local_start.minute
        end_minutes = local_end.hour * 60 + local_end.minute
        if local_end.date() != local_start.date():
            end_minutes += 24 * 60
        if local_start.weekday() < 4:
            window_idx = _window_index(start_minutes, end_minutes, windows)
            if window_idx is not None and not overlaps_any(cursor, cursor + delta, all_busy):
                key = (local_start.date(), window_idx)
                # Prefer slots starting on :00 or :30, then earlier in the window.
                priority = (0 if local_start.minute in (0, 30) else 1, local_start)
                existing = best_by_slot.get(key)
                if existing is None or priority < existing[0]:
                    best_by_slot[key] = (priority, (cursor, cursor + delta))
        cursor += step
    return [pair for _, pair in sorted(best_by_slot.values(), key=lambda v: v[1][0])]


def build_event_link(start, end, title, attendees, details=""):
    """Google Calendar 'render' URL — clicking it pre-fills a new event with attendees."""
    fmt = "%Y%m%dT%H%M%SZ"
    start_utc = start.astimezone(timezone.utc).strftime(fmt)
    end_utc = end.astimezone(timezone.utc).strftime(fmt)
    params = {
        "action": "TEMPLATE",
        "text": title,
        "dates": f"{start_utc}/{end_utc}",
        "add": ",".join(attendees),
        "details": details,
    }
    return "https://calendar.google.com/calendar/render?" + urlencode(params)


def recent_unreplied_pairs(slack, channel_id, lookback_minutes):
    oldest = (datetime.now(timezone.utc) - timedelta(minutes=lookback_minutes)).timestamp()
    resp = slack.conversations_history(channel=channel_id, oldest=str(oldest))
    for msg in resp["messages"]:
        if msg.get("reply_count", 0) > 0:
            continue
        ids = PAIR_MENTION_RE.findall(msg.get("text", ""))
        if len(ids) == 2:
            yield msg["ts"], ids


def slack_ids_to_emails(slack, user_ids):
    """Return (emails, missing) — missing is the list of ids we couldn't resolve."""
    emails, missing = [], []
    for uid in user_ids:
        try:
            profile = slack.users_info(user=uid)["user"].get("profile", {})
        except SlackApiError:
            missing.append(uid)
            continue
        email = profile.get("email")
        if email:
            emails.append(email)
        else:
            missing.append(uid)
    return emails, missing


def post_thread(slack, channel_id, thread_ts, text, dry_run=False):
    if dry_run:
        print(f"[dry-run] would post to {channel_id} thread {thread_ts}:")
        for line in text.splitlines():
            print(f"    {line}")
        return
    try:
        slack.chat_postMessage(channel=channel_id, thread_ts=thread_ts, text=text, unfurl_links=False)
    except SlackApiError as e:
        print(f"Error posting thread reply: {e}")


def reply_with_slot(slack, service, channel_id, thread_ts, user_ids, dry_run=False):
    emails, missing = slack_ids_to_emails(slack, user_ids)
    if missing:
        mention_list = ", ".join(f"<@{u}>" for u in missing)
        post_thread(
            slack, channel_id, thread_ts,
            f"Couldn't look up an email for {mention_list} — skipping slot suggestion.",
            dry_run=dry_run,
        )
        return

    now = datetime.now(timezone.utc).replace(microsecond=0)
    window_end = now + timedelta(days=LOOKAHEAD_DAYS)
    try:
        busy = fetch_busy_intervals(service, emails, now, window_end, TIMEZONE)
    except RuntimeError as e:
        post_thread(slack, channel_id, thread_ts, f"Couldn't read calendars: {e}", dry_run=dry_run)
        return

    slots = find_common_slots(busy, now, window_end, SLOT_MINUTES, COFFEE_WINDOWS, TIMEZONE)
    if not slots:
        post_thread(
            slack, channel_id, thread_ts,
            f"No common 30-min slot in the next {LOOKAHEAD_DAYS} days during coffee windows.",
            dry_run=dry_run,
        )
        return

    lines = [f"Suggested time slots (all times {TIMEZONE}), click a link to create a google calendar event:", ""]
    for i, (start, end) in enumerate(slots[:5], start=1):
        s = start.astimezone(ZoneInfo(TIMEZONE)).strftime("%a %b %d, %H:%M")
        link = build_event_link(start, end, EVENT_TITLE, emails, EVENT_DETAILS)
        lines.append(f"{i}. <{link}|{s}>")
    post_thread(slack, channel_id, thread_ts, "\n".join(lines), dry_run=dry_run)


def main(env):
    dry_run = env == "dev"
    slack = WebClient(token=os.environ["SLACK_API_TOKEN"])
    service = build_calendar_service(TokenFileAuthProvider())
    channel_id = os.environ["SLACK_CHANNEL"]

    pairs = list(recent_unreplied_pairs(slack, channel_id, LOOKBACK_MINUTES))
    mode = "dry-run" if dry_run else "live"
    print(f"[{mode}] Found {len(pairs)} unreplied pair message(s) in last {LOOKBACK_MINUTES} min.")
    for ts, ids in pairs:
        print(f"  {ts}: {ids}")
        reply_with_slot(slack, service, channel_id, ts, ids, dry_run=dry_run)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", choices=["dev", "real"], required=True)
    args = parser.parse_args()
    main(args.env)
