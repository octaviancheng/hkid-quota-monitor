"""Tests for targeted GitHub Issue quota alerts."""

import io
import json
import os
import sys
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from quota_monitor import github_notify as G

HKT = timezone(timedelta(hours=8))
T0 = datetime(2026, 9, 4, 12, 0, tzinfo=HKT)
ALL_OFFICES = ["RHK", "RKO", "RTK", "FTO", "TMO", "YLO"]
CFG = {
    "start_date": "2026-10-07",
    "end_date": "2026-10-10",
    "offices": ALL_OFFICES,
    "sessions": ["R", "K"],
    "cooldown_minutes": 360,
}


def event(office="RHK", day="2026-10-07", session="R", status="g",
          kind="quota_open"):
    return {"type": kind, "office": office, "date": day, "session": session,
            "from": "r", "to": status, "detected_at": T0.isoformat()}


def write_inputs(tmp_path, *, quota=None, events=None, state=None):
    config_path = tmp_path / "config.json"
    quota_path = tmp_path / "quota.json"
    events_path = tmp_path / "events.json"
    state_path = tmp_path / "github_issue_state.json"
    config_path.write_text(json.dumps({"issue_alert": CFG}), encoding="utf-8")
    quota_path.write_text(json.dumps(quota or {"quota": {}, "source_update_time": "x"}),
                          encoding="utf-8")
    events_path.write_text(json.dumps({"events": events or []}), encoding="utf-8")
    if state is not None:
        state_path.write_text(json.dumps(state), encoding="utf-8")
    return config_path, quota_path, events_path, state_path


def run_paths(paths, creator, now=T0):
    return G.run(config_path=paths[0], quota_path=paths[1],
                 events_path=paths[2], state_path=paths[3], now=now,
                 issue_creator=creator)


def test_date_range_is_inclusive():
    assert G.event_matches(event(day="2026-10-07"), CFG)
    assert G.event_matches(event(day="2026-10-10"), CFG)
    assert not G.event_matches(event(day="2026-10-06"), CFG)
    assert not G.event_matches(event(day="2026-10-11"), CFG)


@pytest.mark.parametrize("office", ALL_OFFICES)
@pytest.mark.parametrize("session", ["R", "K"])
def test_all_offices_and_sessions_match(office, session):
    assert G.event_matches(event(office=office, session=session), CFG)


@pytest.mark.parametrize("status", ["r", "x", None])
def test_closed_or_unavailable_status_does_not_match(status):
    assert not G.event_matches(event(status=status), CFG)


def test_first_run_reports_current_open_quota(tmp_path, monkeypatch):
    quota = {"quota": {"RHK": {"2026-10-07": {"R": "g", "K": "x"}}},
             "source_update_time": "09/04/2026 12:00:00"}
    paths = write_inputs(tmp_path, quota=quota)
    monkeypatch.setenv("GITHUB_REPOSITORY", "octocat/hkid-quota-monitor")
    monkeypatch.setenv("GITHUB_REPOSITORY_OWNER", "octocat")
    monkeypatch.setenv("GITHUB_TOKEN", "secret")
    calls = []

    def creator(*args):
        calls.append(args)
        return {"html_url": "https://github.test/issues/1"}

    assert run_paths(paths, creator) == "https://github.test/issues/1"
    assert len(calls) == 1
    assert "2026-10-07" in calls[0][3]
    assert "@octocat" in calls[0][4]
    state = json.loads(paths[3].read_text(encoding="utf-8"))
    assert state["initialized"] is True
    assert "RHK|2026-10-07|R" in state["cell_last_notified"]


def test_first_run_without_open_quota_initializes_without_issue(tmp_path):
    paths = write_inputs(tmp_path)
    called = False

    def creator(*args):
        nonlocal called
        called = True

    assert run_paths(paths, creator) is None
    assert called is False
    assert json.loads(paths[3].read_text(encoding="utf-8"))["initialized"] is True


def test_unchanged_run_does_not_send(tmp_path):
    state = {"version": 1, "initialized": True, "cell_last_notified": {}}
    paths = write_inputs(tmp_path, state=state)
    assert run_paths(paths, lambda *args: pytest.fail("unexpected issue")) is None


def test_cooldown_suppresses_and_then_allows_reopening(tmp_path, monkeypatch):
    key = "RHK|2026-10-07|R"
    state = {"version": 1, "initialized": True,
             "cell_last_notified": {key: T0.isoformat()}}
    paths = write_inputs(tmp_path, events=[event()], state=state)
    monkeypatch.setenv("GITHUB_REPOSITORY", "octocat/repo")
    monkeypatch.setenv("GITHUB_REPOSITORY_OWNER", "octocat")
    monkeypatch.setenv("GITHUB_TOKEN", "secret")
    assert run_paths(paths, lambda *args: pytest.fail("inside cooldown"),
                     now=T0 + timedelta(minutes=359)) is None
    assert run_paths(paths, lambda *args: {"html_url": "https://github.test/issues/2"},
                     now=T0 + timedelta(minutes=361)) == "https://github.test/issues/2"


def test_multiple_matches_create_one_aggregated_issue(tmp_path, monkeypatch):
    state = {"version": 1, "initialized": True, "cell_last_notified": {}}
    paths = write_inputs(tmp_path, events=[event(), event("YLO", "2026-10-10", "K", "y")],
                         state=state)
    monkeypatch.setenv("GITHUB_REPOSITORY", "octocat/repo")
    monkeypatch.setenv("GITHUB_REPOSITORY_OWNER", "octocat")
    monkeypatch.setenv("GITHUB_TOKEN", "secret")
    calls = []

    def creator(*args):
        calls.append(args)
        return {"html_url": "https://github.test/issues/3"}

    run_paths(paths, creator)
    assert len(calls) == 1
    assert "2 matches" in calls[0][3]
    assert "Wan Chai" in calls[0][4] and "Yuen Long" in calls[0][4]


def test_failed_issue_does_not_advance_state(tmp_path, monkeypatch):
    state = {"version": 1, "initialized": True, "cell_last_notified": {}}
    paths = write_inputs(tmp_path, events=[event()], state=state)
    monkeypatch.setenv("GITHUB_REPOSITORY", "octocat/repo")
    monkeypatch.setenv("GITHUB_REPOSITORY_OWNER", "octocat")
    monkeypatch.setenv("GITHUB_TOKEN", "secret")
    before = paths[3].read_text(encoding="utf-8")

    def failure(*args):
        raise RuntimeError("GitHub unavailable")

    with pytest.raises(RuntimeError, match="unavailable"):
        run_paths(paths, failure)
    assert paths[3].read_text(encoding="utf-8") == before


class Response:
    status = 201

    def __init__(self, body=b'{"html_url":"https://github.test/issues/4"}'):
        self.body = body

    def read(self, size=-1):
        return self.body[:size]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_post_issue_success_and_assignment():
    captured = {}

    def opener(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return Response()

    result = G.post_issue("octocat/repo", "token", "octocat", "title", "body", opener)
    payload = json.loads(captured["request"].data)
    assert result["html_url"].endswith("/4")
    assert payload["assignees"] == ["octocat"]
    assert captured["request"].get_header("Authorization") == "Bearer token"


@pytest.mark.parametrize("status", [401, 403])
def test_post_issue_auth_and_rate_errors(status):
    def opener(request, timeout):
        raise urllib.error.HTTPError(request.full_url, status, "failed", {}, io.BytesIO())

    with pytest.raises(RuntimeError, match=f"HTTP {status}"):
        G.post_issue("octocat/repo", "token", "octocat", "title", "body", opener)


def test_post_issue_network_error():
    def opener(request, timeout):
        raise urllib.error.URLError("offline")

    with pytest.raises(RuntimeError, match="offline"):
        G.post_issue("octocat/repo", "token", "octocat", "title", "body", opener)
