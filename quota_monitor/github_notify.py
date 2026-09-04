"""Send targeted HKID quota alerts as GitHub Issues.

The workflow's repository-scoped GITHUB_TOKEN creates one issue per detection
wave. Assigning and mentioning the repository owner lets GitHub deliver both
email and mobile notifications without an external mail provider.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

HKT = timezone(timedelta(hours=8))
DATA = Path("data")
CONFIG_PATH = Path("config.json")
QUOTA_PATH = DATA / "quota.json"
EVENTS_PATH = DATA / "events.json"
STATE_PATH = DATA / "github_issue_state.json"

BOOKING_URL = "https://www.gov.hk/en/residents/immigration/idcard/hkic/bookregidcard.htm"
OPEN = {"g", "y"}
OPEN_EVENT_TYPES = {"quota_open", "new_date", "initial_open"}
OFFICE_NAMES = {
    "RHK": "Wan Chai (灣仔)",
    "RKO": "Cheung Sha Wan (長沙灣)",
    "RTK": "Tseung Kwan O (將軍澳)",
    "FTO": "Fo Tan (火炭)",
    "TMO": "Tuen Mun (屯門)",
    "YLO": "Yuen Long (元朗)",
}
SESSION_NAMES = {"R": "Regular", "K": "Extended"}
STATUS_NAMES = {"g": "Available", "y": "Limited"}
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


class ConfigError(RuntimeError):
    """Raised when the issue alert configuration is unsafe or incomplete."""


def _valid_iso_date(value: object) -> bool:
    if not isinstance(value, str) or not _ISO_DATE.fullmatch(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def load_config(path: Path = CONFIG_PATH) -> dict:
    """Load and strictly validate the targeted issue-alert configuration."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        cfg = raw["issue_alert"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ConfigError(f"cannot load issue_alert configuration: {exc}") from exc

    start = cfg.get("start_date")
    end = cfg.get("end_date")
    if not _valid_iso_date(start) or not _valid_iso_date(end) or start > end:
        raise ConfigError("issue_alert start_date/end_date must be valid inclusive ISO dates")

    offices = cfg.get("offices")
    sessions = cfg.get("sessions")
    cooldown = cfg.get("cooldown_minutes")
    if (not isinstance(offices, list) or not offices
            or any(o not in OFFICE_NAMES for o in offices)
            or len(set(offices)) != len(offices)):
        raise ConfigError("issue_alert offices must be a non-empty, unique list of known office IDs")
    if (not isinstance(sessions, list) or not sessions
            or any(s not in SESSION_NAMES for s in sessions)
            or len(set(sessions)) != len(sessions)):
        raise ConfigError("issue_alert sessions must be a non-empty, unique list of R/K")
    if not isinstance(cooldown, int) or isinstance(cooldown, bool) or cooldown < 0:
        raise ConfigError("issue_alert cooldown_minutes must be a non-negative integer")

    return {
        "start_date": start,
        "end_date": end,
        "offices": offices,
        "sessions": sessions,
        "cooldown_minutes": cooldown,
    }


def event_key(event: dict) -> str:
    return f'{event["office"]}|{event["date"]}|{event["session"]}'


def event_matches(event: dict, cfg: dict) -> bool:
    """Return whether an opening event is inside the configured target."""
    return (
        event.get("type") in OPEN_EVENT_TYPES
        and event.get("to") in OPEN
        and event.get("office") in cfg["offices"]
        and event.get("session") in cfg["sessions"]
        and cfg["start_date"] <= event.get("date", "") <= cfg["end_date"]
    )


def initial_open_events(snapshot: dict, cfg: dict, now: datetime) -> list[dict]:
    """Synthesize opening events for matching quota already open at setup."""
    events: list[dict] = []
    for office in cfg["offices"]:
        by_date = snapshot.get("quota", {}).get(office, {})
        for day, cell in by_date.items():
            if not cfg["start_date"] <= day <= cfg["end_date"]:
                continue
            for session in cfg["sessions"]:
                status = cell.get(session, "x")
                if status in OPEN:
                    events.append({
                        "type": "initial_open",
                        "office": office,
                        "date": day,
                        "session": session,
                        "from": None,
                        "to": status,
                        "detected_at": now.isoformat(timespec="seconds"),
                    })
    return sorted(events, key=lambda e: (e["date"], e["office"], e["session"]))


def load_state(path: Path = STATE_PATH) -> dict:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if (isinstance(state, dict)
                and isinstance(state.get("cell_last_notified", {}), dict)):
            state.setdefault("initialized", False)
            state.setdefault("cell_last_notified", {})
            return state
    except FileNotFoundError:
        pass
    except (OSError, ValueError, TypeError) as exc:
        print(f"WARN issue state unreadable ({exc}); using an empty state")
    return {"version": 1, "initialized": False, "cell_last_notified": {}}


def save_state(state: dict, path: Path = STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
    temporary.replace(path)


def fresh_events(events: list[dict], state: dict, cfg: dict,
                 now: datetime) -> list[dict]:
    """Filter target events and enforce the per-cell cooldown without mutation."""
    cutoff = timedelta(minutes=cfg["cooldown_minutes"])
    seen: set[str] = set()
    fresh: list[dict] = []
    last_notified = state.get("cell_last_notified", {})
    for event in sorted(events, key=lambda e: (
            e.get("date", ""), e.get("office", ""), e.get("session", ""))):
        if not event_matches(event, cfg):
            continue
        key = event_key(event)
        if key in seen:
            continue
        seen.add(key)
        last = last_notified.get(key)
        if last:
            try:
                if now - datetime.fromisoformat(last) < cutoff:
                    continue
            except (TypeError, ValueError):
                pass
        fresh.append(event)
    return fresh


def build_issue(events: list[dict], owner: str, cfg: dict,
                source_update_time: str | None = None) -> tuple[str, str]:
    earliest = min(e["date"] for e in events)
    title = f"🚨 HKID quota available: {earliest} ({len(events)} match"
    title += "es)" if len(events) != 1 else ")"
    rows = []
    for event in events:
        rows.append(
            f'| {event["date"]} | {OFFICE_NAMES[event["office"]]} | '
            f'{SESSION_NAMES[event["session"]]} | {STATUS_NAMES[event["to"]]} |'
        )
    source_line = (f"\nOfficial data timestamp: `{source_update_time}`\n"
                   if source_update_time else "")
    body = f"""@{owner} matching HKID appointment quota has been detected.

| Date | Office | Session | Quota |
|---|---|---|---|
{chr(10).join(rows)}

Target range: **{cfg['start_date']} through {cfg['end_date']} inclusive**.
{source_line}
[Open the official HKID booking service]({BOOKING_URL})

Appointments can disappear quickly. This is a third-party availability alert and does not make a booking.
"""
    return title, body


def _response_json(response) -> dict:
    raw = response.read(65536).decode("utf-8", "replace")
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise RuntimeError("GitHub returned a non-JSON response") from exc
    if not isinstance(payload, dict) or not payload.get("html_url"):
        raise RuntimeError("GitHub issue response did not contain html_url")
    return payload


def post_issue(repo: str, token: str, owner: str, title: str, body: str,
               opener: Callable = urllib.request.urlopen) -> dict:
    if not repo or not token or not owner:
        raise RuntimeError("GITHUB_REPOSITORY, GITHUB_TOKEN and GITHUB_REPOSITORY_OWNER are required")
    payload = json.dumps({
        "title": title,
        "body": body,
        "assignees": [owner],
    }, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/issues",
        data=payload,
        method="POST",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "hkid-quota-monitor",
        },
    )
    try:
        with opener(request, timeout=30) as response:
            if getattr(response, "status", 0) != 201:
                raise RuntimeError(f"GitHub issue creation returned HTTP {response.status}")
            return _response_json(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"GitHub issue creation failed with HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"GitHub issue creation failed: {exc.reason}") from exc


def run(*, config_path: Path = CONFIG_PATH, quota_path: Path = QUOTA_PATH,
        events_path: Path = EVENTS_PATH, state_path: Path = STATE_PATH,
        now: datetime | None = None, issue_creator: Callable | None = None) -> str | None:
    """Send a live alert if needed and persist state only after successful delivery."""
    now = now or datetime.now(HKT)
    cfg = load_config(config_path)
    state = load_state(state_path)
    snapshot = json.loads(quota_path.read_text(encoding="utf-8"))

    if not state.get("initialized"):
        candidates = initial_open_events(snapshot, cfg, now)
    else:
        payload = json.loads(events_path.read_text(encoding="utf-8"))
        candidates = payload.get("events", [])

    fresh = fresh_events(candidates, state, cfg, now)
    next_state = copy.deepcopy(state)
    next_state["version"] = 1
    next_state["initialized"] = True

    if fresh:
        owner = os.environ.get("GITHUB_REPOSITORY_OWNER", "")
        title, body = build_issue(fresh, owner, cfg,
                                  snapshot.get("source_update_time"))
        creator = issue_creator or post_issue
        result = creator(
            os.environ.get("GITHUB_REPOSITORY", ""),
            os.environ.get("GITHUB_TOKEN", ""),
            owner,
            title,
            body,
        )
        stamp = now.isoformat(timespec="seconds")
        for event in fresh:
            next_state["cell_last_notified"][event_key(event)] = stamp
        save_state(next_state, state_path)
        url = result.get("html_url") if isinstance(result, dict) else None
        print(f"GitHub issue alert sent: {url or '(URL unavailable)'}")
        return url

    if next_state != state:
        save_state(next_state, state_path)
    print("No matching HKID quota openings to notify")
    return None


def send_test_issue(issue_creator: Callable | None = None) -> str:
    """Send an unmistakable test issue without reading or changing live state."""
    cfg = load_config()
    owner = os.environ.get("GITHUB_REPOSITORY_OWNER", "")
    title = "🧪 TEST: HKID quota notifications are working"
    body = f"""@{owner} this is a **test notification only**.

No appointment availability was detected and no booking has been made.

The live monitor watches all six offices and both sessions from **{cfg['start_date']} through {cfg['end_date']} inclusive**.
"""
    creator = issue_creator or post_issue
    result = creator(
        os.environ.get("GITHUB_REPOSITORY", ""),
        os.environ.get("GITHUB_TOKEN", ""),
        owner,
        title,
        body,
    )
    url = result.get("html_url", "")
    print(f"GitHub test issue sent: {url or '(URL unavailable)'}")
    return url


def main(argv: list[str] | None = None) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true",
                        help="create an unmistakable test issue and do not change alert state")
    args = parser.parse_args(argv)
    if args.test:
        send_test_issue()
    else:
        run()


if __name__ == "__main__":
    main()
