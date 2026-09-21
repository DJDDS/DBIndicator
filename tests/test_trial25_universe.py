import datetime as dt

from app import trial25_universe as u


def _c(symbol, expiry="2026-10-27", typ="CE"):
    return {
        "name": symbol,
        "underlying": symbol,
        "instrument_type": typ,
        "expiry": dt.date.fromisoformat(expiry),
        "tradingsymbol": f"{symbol}{expiry.replace('-', '')}{typ}",
    }


def test_new_fno_name_is_onboarding_not_primary():
    contracts = {
        "OLD": [_c("OLD"), _c("OLD", typ="PE")],
        "NEWCO": [_c("NEWCO"), _c("NEWCO", typ="PE")],
    }
    out = u.reconcile_universe({"OLD"}, contracts, {}, dt.datetime(2026, 9, 30, 9, 20))
    assert out["cohort_a_live"] == ["OLD"]
    assert out["new_fno_onboarding"] == ["NEWCO"]
    assert "NEWCO" not in out["cohort_a_live"]


def test_removed_frozen_name_is_recorded_not_replaced():
    contracts = {"OLD": [_c("OLD"), _c("OLD", typ="PE")]}
    out = u.reconcile_universe(
        {"OLD", "REMOVED"}, contracts, {}, dt.datetime(2026, 10, 1, 9, 20)
    )
    assert out["cohort_a_live"] == ["OLD"]
    assert out["cohort_a_missing_contracts"] == ["REMOVED"]


def test_phaseout_name_remains_live_while_unexpired_option_contract_exists():
    contracts = {
        "PHASE": [
            _c("PHASE", "2026-10-27", "CE"),
            _c("PHASE", "2026-10-27", "PE"),
        ]
    }
    out = u.reconcile_universe(
        {"PHASE"}, contracts, {}, dt.datetime(2026, 9, 30, 9, 20)
    )
    assert out["cohort_a_live"] == ["PHASE"]
    assert out["cohort_a_missing_contracts"] == []


def test_expired_only_contracts_do_not_count_as_current_fno():
    contracts = {
        "OLD": [
            _c("OLD", "2026-09-29", "CE"),
            _c("OLD", "2026-09-29", "PE"),
        ]
    }
    out = u.reconcile_universe(
        {"OLD"}, contracts, {}, dt.datetime(2026, 9, 30, 9, 20)
    )
    assert out["cohort_a_live"] == []
    assert out["cohort_a_missing_contracts"] == ["OLD"]


def test_first_and_last_seen_are_persistent_across_reconciliation():
    prior = {
        "first_seen": {"OLD": "2026-09-29T09:20:00"},
        "last_seen": {"OLD": "2026-09-29T09:20:00"},
    }
    contracts = {
        "OLD": [_c("OLD"), _c("OLD", typ="PE")],
        "NEWCO": [_c("NEWCO"), _c("NEWCO", typ="PE")],
    }
    now = dt.datetime(2026, 9, 30, 9, 20)
    out = u.reconcile_universe({"OLD"}, contracts, prior, now)
    assert out["first_seen"]["OLD"] == "2026-09-29T09:20:00"
    assert out["first_seen"]["NEWCO"] == now.isoformat(timespec="seconds")
    assert out["last_seen"]["OLD"] == now.isoformat(timespec="seconds")


def test_reconciliation_state_is_persisted_atomically(tmp_path):
    state_file = tmp_path / "onboarding.json"
    contracts = {
        "OLD": [_c("OLD"), _c("OLD", typ="PE")],
        "NEWCO": [_c("NEWCO"), _c("NEWCO", typ="PE")],
    }
    now = dt.datetime(2026, 9, 30, 9, 20)
    out = u.update_universe_state(state_file, {"OLD"}, contracts, now)
    assert out["cohort_a_live"] == ["OLD"]
    assert out["new_fno_onboarding"] == ["NEWCO"]
    restored = u.load_universe_state(state_file)
    assert restored == out
    assert not (tmp_path / "onboarding.json.tmp").exists()


def test_reconciliation_preserves_first_seen_across_restarts(tmp_path):
    state_file = tmp_path / "onboarding.json"
    day1 = dt.datetime(2026, 9, 30, 9, 20)
    day2 = dt.datetime(2026, 10, 1, 9, 20)
    contracts = {"NEWCO": [_c("NEWCO"), _c("NEWCO", typ="PE")]}
    first = u.update_universe_state(state_file, set(), contracts, day1)
    second = u.update_universe_state(state_file, set(), contracts, day2)
    assert second["first_seen"]["NEWCO"] == first["first_seen"]["NEWCO"]
    assert second["last_seen"]["NEWCO"] == day2.isoformat(timespec="seconds")
