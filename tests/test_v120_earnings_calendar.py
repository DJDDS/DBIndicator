import datetime as dt
import json

from app.v12_earnings_calendar import (
    fetch_upcoming_earnings,
    parse_bulk_board_meetings,
    record_calendar_observation,
    upcoming_earnings_symbols,
)


def _payload():
    return {
        "data": [
            {
                "symbol": "AAA",
                "purpose": "To consider and approve unaudited financial results",
                "meetingDate": "20-Sep-2026",
                "broadcastDate": "05-Sep-2026 10:15:00",
                "details": "Quarterly results",
            },
            {
                "symbol": "BBB",
                "purpose": "Fund raising / other business matters",
                "meetingDate": "21-Sep-2026",
            },
            {
                "symbol": "CCC",
                "purpose": "Audited Results for the quarter",
                "meetingDate": "22-Sep-2026",
            },
            {
                "symbol": "NONFNO",
                "purpose": "Quarterly Financial Result",
                "meetingDate": "23-Sep-2026",
            },
        ]
    }


def test_bulk_parser_keeps_only_financial_result_purposes_and_fno_symbols():
    rows = parse_bulk_board_meetings(_payload(), fno_symbols={"AAA", "BBB", "CCC"})
    assert [r["symbol"] for r in rows] == ["AAA", "CCC"]
    assert rows[0]["meeting_date"] == "2026-09-20"
    assert rows[0]["broadcast_at"].startswith("2026-09-05")
    assert rows[0]["source_fingerprint"]


def test_point_in_time_ledger_tracks_first_seen_unchanged_and_revision(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    state_file = tmp_path / "state.json"
    now1 = dt.datetime(2026, 9, 5, 10, 30)
    events = parse_bulk_board_meetings(_payload(), fno_symbols={"AAA", "CCC"})
    state1 = record_calendar_observation(events, now=now1, ledger_file=ledger, state_file=state_file)
    assert state1["events"]["AAA"]["first_seen_at"] == "2026-09-05T10:30:00"
    assert state1["events"]["AAA"]["meeting_date"] == "2026-09-20"
    assert state1["events"]["AAA"]["state"] == "ACTIVE"

    now2 = dt.datetime(2026, 9, 6, 10, 30)
    state2 = record_calendar_observation(events, now=now2, ledger_file=ledger, state_file=state_file)
    assert state2["events"]["AAA"]["first_seen_at"] == "2026-09-05T10:30:00"
    assert state2["events"]["AAA"]["last_seen_at"] == "2026-09-06T10:30:00"

    revised = [dict(x) for x in events]
    revised[0]["meeting_date"] = "2026-09-21"
    state3 = record_calendar_observation(revised, now=dt.datetime(2026, 9, 7, 10, 30), ledger_file=ledger, state_file=state_file)
    assert state3["events"]["AAA"]["meeting_date"] == "2026-09-21"
    assert state3["events"]["AAA"]["last_changed_at"] == "2026-09-07T10:30:00"
    assert state3["events"]["AAA"]["state"] == "REVISED"

    records = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
    assert any(r["change_type"] == "FIRST_SEEN" and r["symbol"] == "AAA" for r in records)
    assert any(r["change_type"] == "REVISED" and r["symbol"] == "AAA" for r in records)


def test_disappeared_future_event_is_preserved_as_removed_not_deleted(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    state_file = tmp_path / "state.json"
    events = parse_bulk_board_meetings(_payload(), fno_symbols={"AAA", "CCC"})
    record_calendar_observation(events, now=dt.datetime(2026, 9, 5, 10, 30), ledger_file=ledger, state_file=state_file)
    state = record_calendar_observation([events[1]], now=dt.datetime(2026, 9, 6, 10, 30), ledger_file=ledger, state_file=state_file)
    assert state["events"]["AAA"]["state"] == "REMOVED"
    assert state["events"]["AAA"]["meeting_date"] == "2026-09-20"


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload
    def raise_for_status(self):
        return None
    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, payload=None, fail=False):
        self.payload = payload
        self.fail = fail
        self.calls = []
        self.headers = {}
    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.fail:
            raise RuntimeError("NSE unavailable")
        return FakeResponse(self.payload)


def test_bulk_fetch_uses_one_board_meeting_request_and_filters_fno():
    session = FakeSession(_payload())
    out = fetch_upcoming_earnings(session, {"AAA", "CCC"}, dt.date(2026, 9, 5), dt.date(2026, 10, 20))
    assert out["status"] == "OK"
    assert [x["symbol"] for x in out["events"]] == ["AAA", "CCC"]
    api_calls = [x for x in session.calls if "corporate-board-meetings" in x[0]]
    assert len(api_calls) == 1
    params = api_calls[0][1]["params"]
    assert params["index"] == "equities"
    assert params["fno"] == "true"


def test_fetch_failure_is_unavailable_and_never_infers_dates():
    out = fetch_upcoming_earnings(FakeSession(fail=True), {"AAA"}, dt.date(2026, 9, 5), dt.date(2026, 10, 20))
    assert out["status"] == "UNAVAILABLE"
    assert out["events"] == []
    assert "NSE unavailable" in out["error"]


def test_upcoming_earnings_symbols_uses_active_current_state_only():
    state = {
        "events": {
            "AAA": {"meeting_date": "2026-09-09", "state": "ACTIVE"},
            "BBB": {"meeting_date": "2026-09-20", "state": "ACTIVE"},
            "CCC": {"meeting_date": "2026-09-08", "state": "REMOVED"},
        }
    }
    assert upcoming_earnings_symbols(state, dt.date(2026, 9, 5), days=7) == {"AAA"}


def test_iso_dates_do_not_emit_dayfirst_warning():
    import warnings
    from app import v12_earnings_calendar as cal
    with warnings.catch_warnings():
        warnings.simplefilter('error', UserWarning)
        assert cal._parse_date('2026-09-08').isoformat() == '2026-09-08'


def test_bulk_fetch_warms_nse_session_before_api_request():
    session = FakeSession(_payload())
    out = fetch_upcoming_earnings(session, {'AAA'}, dt.date(2026, 9, 5), dt.date(2026, 10, 20))
    assert out['status'] == 'OK'
    assert session.calls[0][0] == 'https://www.nseindia.com/'
