import sys
import types

if "kiteconnect" not in sys.modules:
    mod = types.ModuleType("kiteconnect")
    mod.KiteConnect = type("KiteConnect", (), {})
    mod.KiteTicker = type("KiteTicker", (), {})
    sys.modules["kiteconnect"] = mod

from app import background


def test_apply_derivative_intelligence_delegates_to_live_option_layer(monkeypatch):
    rows = [{"symbol":"ABC","v8_direction":"Bullish","v8_state":"TRADE CANDIDATE","v8_decision_score":92,"close":100}]
    seen = {}
    def fake(kite, passed, **kwargs):
        seen['kite'] = kite
        seen['rows'] = passed
        passed[0]['option_action'] = 'OPTION BUYER EDGE'
        return passed
    monkeypatch.setattr(background, 'derivative_intelligence', type('DI', (), {'enrich_shortlisted_options': staticmethod(fake)}))
    background._apply_derivative_intelligence(object(), rows, now="2026-08-29T14:30:00")
    assert seen['rows'] is rows
    assert rows[0]['option_action'] == 'OPTION BUYER EDGE'
