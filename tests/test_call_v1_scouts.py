from app import background, v122b_tactical


def _ev(sym, z, direction="Bullish", p=1e-4):
    return {"symbol": sym, "direction": direction, "movement_z": z, "movement_p_value": p,
            "movement_familywise_alpha": 0.01, "live_price": 100.0}


def test_scouts_fill_free_slots_with_strongest_bullish_movers():
    focus_rows = [{"symbol": f"F{i}", "direction": "Bearish"} for i in range(3)]
    observer = {"events": [_ev("A", 4.0), _ev("B", 9.0), _ev("C", 6.0), _ev("D", 5.0), _ev("E", 7.0),
                           _ev("BEAR", 12.0, direction="Bearish"), _ev("WEAK", 20.0, p=0.5), _ev("F0", 30.0)]}
    out = background._with_call_v1_scouts(focus_rows, {"continuation_watch": {}}, observer, [], set())
    syms = [r["symbol"] for r in out]
    assert syms[:3] == ["F0", "F1", "F2"]                       # Focus first, unchanged
    scouts = [r for r in out if r.get("flow_scout")]
    # alternating sides, bullish first, each side by |z|; max 4
    assert [r["symbol"] for r in scouts] == ["B", "BEAR", "E", "C"]
    assert [r["symbol"] for r in out if r.get("call_v1_scout")] == ["B", "E", "C"]
    bear = next(r for r in scouts if r["symbol"] == "BEAR")
    assert bear["direction"] == "Bearish" and bear["scout_side"] == "PUT" and not bear["call_v1_scout"]
    assert len(out) <= v122b_tactical.TACTICAL_POOL_MAX


def test_bullish_only_day_keeps_all_scout_slots_for_call_v1():
    observer = {"events": [_ev("A", 4.0), _ev("B", 9.0), _ev("C", 6.0), _ev("D", 5.0), _ev("E", 7.0)]}
    out = background._with_call_v1_scouts([], {"continuation_watch": {}}, observer, [], set())
    assert [r["symbol"] for r in out if r.get("call_v1_scout")] == ["B", "E", "C", "D"]


def test_scouts_respect_option_feasibility_and_pool_limit():
    focus_rows = [{"symbol": f"F{i}", "direction": "Bullish"} for i in range(v122b_tactical.TACTICAL_POOL_MAX)]
    observer = {"events": [_ev("A", 9.0)]}
    out = background._with_call_v1_scouts(focus_rows, {"continuation_watch": {}}, observer, [], set())
    assert not any(r.get("call_v1_scout") for r in out)          # no free slot
    out = background._with_call_v1_scouts([], {"continuation_watch": {}}, observer, [], {"OTHER"})
    assert out == []                                              # A not option-feasible


def test_scouts_rank_ahead_of_continuation_alumni():
    rows = [{"symbol": "F0"}, {"symbol": "OLD"}]
    out = background._with_call_v1_scouts(rows, {"continuation_watch": {"OLD": {}}}, {"events": [_ev("A", 8.0)]}, [], set())
    assert [r["symbol"] for r in out] == ["F0", "A", "OLD"]
